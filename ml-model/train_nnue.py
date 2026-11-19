"""Train the NNUE-style engine on the same grouped split as XGBoost.

Two stages, so no test row ever influences a choice:
1. Cross-fit over K folds of the training games. Early stopping on each held
   out fold learns how many epochs the networks need. The out-of-fold scores
   (one per training game) are logged as an accuracy/false-positive curve.
   The decision cutoff is ``fixed_threshold`` (0.55 by default). With it set
   to None, the out-of-fold scores pick it instead (see ``choose_threshold``).
2. Refit on all training games for that many epochs. The threshold is folded
   into the output logit, so live workers keep using ``score >= 0.5``.
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn as nn

from model_artifacts import model_artifact_path, save_feature_schema
from nnue_model import (
    NNUE_MODEL_FILENAME,
    NNUEEnsemble,
    NNUEFeatureSpec,
    NNUENetwork,
    build_checkpoint,
)
from training_data import (
    group_train_test_indices,
    prepare_game_move_sequences,
    prepare_game_training_data,
)


@dataclass(frozen=True)
class NNUETrainingConfig:
    seeds: int = 5
    bucket_count: int = 16
    # Lag 2 pairs each move with the same player's previous move.
    transition_lag: int = 2
    accumulator_size: int = 256
    hidden_size: int = 32
    dropout: float = 0.3
    learning_rate: float = 1e-3
    weight_decay: float = 1e-2
    batch_size: int = 256
    max_epochs: int = 200
    patience: int = 20
    # Stage 1 cross-fits over this many folds of the training games.
    validation_folds: int = 5
    # Decision cutoff. 0.55 is a deliberate product choice: the engines rank
    # games about equally, and above 0.5 the NNUE trades a little recall for
    # fewer false accusations. Set None to pick from the out-of-fold scores
    # instead (see choose_threshold).
    fixed_threshold: float | None = 0.55
    # Thresholds above 0.5 trade a little recall for fewer false positives.
    threshold_grid: tuple[float, ...] = tuple(np.round(np.arange(0.5, 0.701, 0.025), 3))


@dataclass
class GameMoveData:
    """Game-level features, labels, and each game's moves, all in one order."""

    features: np.ndarray
    labels: np.ndarray
    feature_schema: list[str]
    moves: np.ndarray
    move_columns: list[str]
    lengths: np.ndarray
    move_starts: np.ndarray
    game_ids: pd.Index

    def moves_for(self, game_index: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        rows = np.concatenate(
            [
                np.arange(self.move_starts[i], self.move_starts[i] + self.lengths[i])
                for i in game_index
            ]
        )
        return self.moves[rows], self.lengths[game_index]


def load_game_move_data(dataframe: pd.DataFrame) -> GameMoveData:
    features, labels = prepare_game_training_data(dataframe)
    moves, lengths, move_columns = prepare_game_move_sequences(dataframe, features.index)
    return GameMoveData(
        features=features.to_numpy(dtype="float32"),
        labels=labels.to_numpy(dtype="float32"),
        feature_schema=features.columns.tolist(),
        moves=moves,
        move_columns=move_columns,
        lengths=lengths,
        move_starts=np.concatenate([[0], np.cumsum(lengths)[:-1]]),
        game_ids=features.index,
    )


def _inputs(spec: NNUEFeatureSpec, data: GameMoveData, game_index: np.ndarray):
    moves, lengths = data.moves_for(game_index)
    return (
        torch.from_numpy(spec.histograms(moves, lengths)),
        torch.from_numpy(spec.dense_features(data.features[game_index])),
        torch.from_numpy(data.labels[game_index]),
    )


def _fit_spec(spec_data: GameMoveData, game_index, config) -> NNUEFeatureSpec:
    moves, _ = spec_data.moves_for(game_index)
    return NNUEFeatureSpec.fit(
        moves,
        spec_data.move_columns,
        spec_data.features[game_index],
        spec_data.feature_schema,
        bucket_count=config.bucket_count,
        transition_lag=config.transition_lag,
    )


def _train_member(
    spec, train_inputs, config, seed, epochs, validation_inputs=None
) -> tuple[NNUENetwork, int]:
    """Train one seed; with validation inputs, stop early and return the best epoch."""
    torch.manual_seed(seed)
    network = NNUENetwork(
        spec.sparse_size,
        spec.dense_size,
        config.accumulator_size,
        config.hidden_size,
        config.dropout,
    )
    optimizer = torch.optim.AdamW(
        network.parameters(), lr=config.learning_rate, weight_decay=config.weight_decay
    )
    # T_max stays at max_epochs in both stages so a refit sees the same schedule.
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, config.max_epochs)
    loss_fn = nn.BCEWithLogitsLoss()
    histograms, dense, labels = train_inputs
    best_loss, best_state, best_epoch, stale = float("inf"), None, epochs, 0

    for epoch in range(1, epochs + 1):
        network.train()
        permutation = torch.randperm(len(labels))
        for start in range(0, len(labels), config.batch_size):
            batch = permutation[start:start + config.batch_size]
            optimizer.zero_grad()
            loss = loss_fn(network(histograms[batch], dense[batch]).squeeze(1), labels[batch])
            loss.backward()
            optimizer.step()
        scheduler.step()

        if validation_inputs is None:
            continue
        network.eval()
        with torch.no_grad():
            validation_loss = loss_fn(
                network(validation_inputs[0], validation_inputs[1]).squeeze(1),
                validation_inputs[2],
            ).item()
        if validation_loss < best_loss:
            best_loss, best_epoch, stale = validation_loss, epoch, 0
            best_state = {key: value.clone() for key, value in network.state_dict().items()}
        else:
            stale += 1
            if stale > config.patience:
                break

    if best_state is not None:
        network.load_state_dict(best_state)
    network.eval()
    return network, best_epoch


def classification_metrics(scores, labels, threshold: float = 0.5) -> dict:
    predictions = np.asarray(scores) >= threshold
    labels = np.asarray(labels).astype(bool)
    negatives = (~labels).sum()
    positives = labels.sum()
    false_positives = (predictions & ~labels).sum()
    true_positives = (predictions & labels).sum()
    return {
        "accuracy": float((predictions == labels).mean()),
        "false_positive_rate": float(false_positives / negatives) if negatives else 0.0,
        "true_positive_rate": float(true_positives / positives) if positives else 0.0,
    }


def choose_threshold(scores, labels, grid) -> float:
    """Lowest-FPR threshold whose accuracy is statistically tied with the best.

    Near the optimum, accuracy is flat while the false positive rate keeps
    falling, so "highest accuracy" alone picks a cutoff from noise. Instead,
    find the most accurate threshold and accept any threshold whose accuracy
    gap is within one paired standard error of it. Paired means only the games
    whose prediction changes between the two thresholds count (McNemar-style):
    sqrt(changed) / n. Among those, the lowest false positive rate wins.
    """
    scores, labels = np.asarray(scores), np.asarray(labels)
    accuracy = {
        value: classification_metrics(scores, labels, value)["accuracy"]
        for value in grid
    }
    best = max(grid, key=lambda value: (accuracy[value], value))
    best_predictions = scores >= best
    tied = []
    for value in grid:
        changed = int(((scores >= value) != best_predictions).sum())
        standard_error = np.sqrt(changed) / len(scores)
        if accuracy[best] - accuracy[value] <= standard_error:
            tied.append(value)
    return min(
        tied,
        key=lambda value: (
            classification_metrics(scores, labels, value)["false_positive_rate"],
            -accuracy[value],
        ),
    )


def fit_nnue(
    data: GameMoveData,
    train_index: np.ndarray,
    config: NNUETrainingConfig = NNUETrainingConfig(),
    log=print,
) -> tuple[NNUEEnsemble, dict]:
    """Run both training stages using only ``train_index`` games."""
    rng = np.random.default_rng(0)
    shuffled = rng.permutation(train_index)
    folds = np.array_split(shuffled, config.validation_folds)

    # Stage 1: out-of-fold scores and best epochs from K cross-fitted ensembles.
    validation_scores, validation_labels, best_epochs = [], [], []
    for fold_number, validation_index in enumerate(folds):
        fit_index = np.concatenate(
            [fold for position, fold in enumerate(folds) if position != fold_number]
        )
        spec = _fit_spec(data, fit_index, config)
        fit_inputs = _inputs(spec, data, fit_index)
        validation_inputs = _inputs(spec, data, validation_index)
        members = []
        for seed in range(config.seeds):
            member, best_epoch = _train_member(
                spec, fit_inputs, config, seed, config.max_epochs, validation_inputs
            )
            members.append(member)
            best_epochs.append(best_epoch)
        validation_scores.append(
            NNUEEnsemble(spec, members).scores(
                validation_inputs[0].numpy(), validation_inputs[1].numpy()
            )
        )
        validation_labels.append(validation_inputs[2].numpy())
        log(f"  stage 1 fold {fold_number + 1}/{len(folds)} done")
    validation_scores = np.concatenate(validation_scores)
    validation_labels = np.concatenate(validation_labels)
    if config.fixed_threshold is None:
        threshold = choose_threshold(
            validation_scores, validation_labels, config.threshold_grid
        )
    else:
        threshold = config.fixed_threshold
    for value in config.threshold_grid:
        metrics = classification_metrics(validation_scores, validation_labels, value)
        log(
            f"  out-of-fold @ {value:.3f}: accuracy {metrics['accuracy']:.4f}, "
            f"false positive rate {metrics['false_positive_rate']:.4f}"
        )
    refit_epochs = int(np.median(best_epochs))
    log(f"  chosen threshold {threshold}, refit epochs {refit_epochs}")

    # Stage 2: refit bucket edges, scaling, and weights on every training game.
    spec = _fit_spec(data, train_index, config)
    train_inputs = _inputs(spec, data, train_index)
    members = [
        _train_member(spec, train_inputs, config, seed, refit_epochs)[0]
        for seed in range(config.seeds)
    ]
    offset = float(np.log(threshold / (1.0 - threshold)))
    ensemble = NNUEEnsemble(spec, members, decision_offset=offset)
    return ensemble, {
        "threshold": float(threshold),
        "refit_epochs": refit_epochs,
        "validation": classification_metrics(validation_scores, validation_labels, threshold),
    }


def score_games(ensemble: NNUEEnsemble, data: GameMoveData, game_index) -> np.ndarray:
    moves, lengths = data.moves_for(game_index)
    return ensemble.scores(
        ensemble.spec.histograms(moves, lengths),
        ensemble.spec.dense_features(data.features[game_index]),
    )


def grouped_split(data: GameMoveData) -> tuple[np.ndarray, np.ndarray]:
    """Reproduce train_xgboost.py's split exactly (it depends on game ID values)."""
    features_frame = pd.DataFrame(
        data.features, columns=data.feature_schema, index=data.game_ids
    )
    game_ids = pd.Series(data.game_ids, index=data.game_ids, dtype="string")
    return group_train_test_indices(
        features_frame, pd.Series(data.labels, index=data.game_ids), game_ids
    )


def main() -> None:
    dataframe = pd.read_csv(Path(__file__).with_name("chess_dataset.csv"))
    data = load_game_move_data(dataframe)
    # Same grouped split and seed as train_xgboost.py, so test games match.
    train_index, test_index = grouped_split(data)

    config = NNUETrainingConfig()
    print("Starting NNUE model training...")
    ensemble, info = fit_nnue(data, train_index, config)
    test_metrics = classification_metrics(
        score_games(ensemble, data, test_index), data.labels[test_index]
    )
    print("NNUE model accuracy: %.2f%%" % (test_metrics["accuracy"] * 100))

    save_feature_schema(data.feature_schema)
    nnue_path = model_artifact_path(NNUE_MODEL_FILENAME)
    torch.save(
        build_checkpoint(
            ensemble,
            config.accumulator_size,
            config.hidden_size,
            {"threshold": info["threshold"], **{f"test_{k}": v for k, v in test_metrics.items()}},
        ),
        nnue_path,
    )
    print(f"Saved NNUE model to {nnue_path}")
    print(f"False Positive rate: {test_metrics['false_positive_rate']}")


if __name__ == "__main__":
    main()

"""Compare the XGBoost, NNUE, and consensus engines on accuracy and false positive rate.

Consensus flags a game as AI only when both XGBoost and NNUE do. Its score is
the minimum of the two, which crosses 0.5 exactly when both scores do.

Default: score the saved artifacts on the shared held-out test split.
``--cv K``: retrain both engines from scratch on K grouped folds, which is a
sturdier comparison than one split because every game is tested exactly once.

XGBoost and PyTorch each bundle an OpenMP runtime, and loading both into one
process can crash on macOS. Like the live pipeline, this script keeps them in
separate processes: XGBoost always runs in a spawned child that never imports
torch, which is why the heavy imports below are inside functions.
"""

import argparse
import multiprocessing
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.model_selection import GroupKFold

from model_artifacts import MODELS_DIRECTORY, load_feature_schema
from model_preprocessing import load_xgboost_preprocessing, prepare_min_max_splits


DATASET_PATH = Path(__file__).with_name("chess_dataset.csv")


def xgboost_classifier():
    from xgboost import XGBClassifier

    # Kept identical to train_xgboost.py so the comparison is like for like.
    return XGBClassifier(
        n_estimators=5000,
        max_depth=12,
        subsample=0.8,
        colsample_bytree=0.8,
        learning_rate=0.01,
        objective="binary:logistic",
    )


def _saved_xgboost_scores(features: np.ndarray) -> np.ndarray:
    from xgboost import XGBClassifier

    preprocessing = load_xgboost_preprocessing(MODELS_DIRECTORY, load_feature_schema())
    model = XGBClassifier()
    model.load_model(MODELS_DIRECTORY / "xgboost_model.json")
    transformed = (features - np.asarray(preprocessing["minimum"])) / np.asarray(
        preprocessing["range"]
    )
    return model.predict_proba(transformed.astype("float32"))[:, 1]


def _fold_xgboost_scores(features, labels, train_index, test_index) -> np.ndarray:
    X_train, X_test, _, _ = prepare_min_max_splits(features, train_index, test_index)
    model = xgboost_classifier().fit(X_train, labels[train_index])
    return model.predict_proba(X_test)[:, 1]


def in_xgboost_process(function, *args) -> np.ndarray:
    """Run an XGBoost-only function in a fresh process without torch loaded."""
    with multiprocessing.get_context("spawn").Pool(1) as pool:
        return pool.apply(function, args)


def classification_metrics(scores, labels, threshold: float = 0.5) -> dict:
    from train_nnue import classification_metrics as metrics

    return metrics(scores, labels, threshold)


def consensus_scores(xgboost_scores, nnue_scores) -> np.ndarray:
    """AND of the two engines under the shared ``score >= 0.5`` rule."""
    return np.minimum(xgboost_scores, nnue_scores)


def confusion_counts(scores, labels, threshold: float = 0.5) -> dict:
    predictions = np.asarray(scores) >= threshold
    labels = np.asarray(labels).astype(bool)
    return {
        "human_games": int((~labels).sum()),
        "human_flagged_ai": int((predictions & ~labels).sum()),
        "ai_games": int(labels.sum()),
        "ai_flagged_ai": int((predictions & labels).sum()),
    }


def print_counts(name: str, counts: dict) -> None:
    print(
        f"{name:<10} human games flagged AI {counts['human_flagged_ai']:>4} / "
        f"{counts['human_games']:<5} AI games flagged AI {counts['ai_flagged_ai']:>5} / "
        f"{counts['ai_games']}"
    )


def print_row(name: str, metrics: dict) -> None:
    print(
        f"{name:<10} accuracy {metrics['accuracy'] * 100:6.2f}%   "
        f"false positive rate {metrics['false_positive_rate'] * 100:5.2f}%   "
        f"true positive rate {metrics['true_positive_rate'] * 100:6.2f}%"
    )


def compare_saved(data) -> None:
    from train_nnue import grouped_split, score_games

    feature_schema = load_feature_schema()
    if feature_schema != data.feature_schema:
        raise ValueError("Saved feature schema does not match the dataset; retrain.")
    _, test_index = grouped_split(data)
    labels = data.labels[test_index]
    xgboost_scores = in_xgboost_process(_saved_xgboost_scores, data.features[test_index])

    import torch
    from nnue_model import NNUE_MODEL_FILENAME, load_ensemble

    checkpoint = torch.load(MODELS_DIRECTORY / NNUE_MODEL_FILENAME, map_location="cpu")
    nnue_scores = score_games(load_ensemble(checkpoint, feature_schema), data, test_index)

    scores = {
        "xgboost": xgboost_scores,
        "nnue": nnue_scores,
        "consensus": consensus_scores(xgboost_scores, nnue_scores),
    }
    print(f"Held-out test split: {len(test_index)} games")
    for name, engine_scores in scores.items():
        print_row(name, classification_metrics(engine_scores, labels))
    for name, engine_scores in scores.items():
        print_counts(name, confusion_counts(engine_scores, labels))


def compare_cross_validated(data, folds: int) -> None:
    from train_nnue import NNUETrainingConfig, fit_nnue, score_games

    splitter = GroupKFold(n_splits=folds)
    totals = {"xgboost": [], "nnue": [], "consensus": []}
    for fold, (train_index, test_index) in enumerate(
        splitter.split(data.features, data.labels, groups=data.game_ids)
    ):
        labels = data.labels[test_index]
        xgboost_scores = in_xgboost_process(
            _fold_xgboost_scores, data.features, data.labels, train_index, test_index
        )
        xgboost_metrics = classification_metrics(xgboost_scores, labels)

        ensemble, _ = fit_nnue(data, train_index, NNUETrainingConfig(), log=lambda _: None)
        nnue_scores = score_games(ensemble, data, test_index)
        nnue_metrics = classification_metrics(nnue_scores, labels)
        consensus_metrics = classification_metrics(
            consensus_scores(xgboost_scores, nnue_scores), labels
        )

        totals["xgboost"].append(xgboost_metrics)
        totals["nnue"].append(nnue_metrics)
        totals["consensus"].append(consensus_metrics)
        print(f"Fold {fold + 1}/{folds}")
        print_row("xgboost", xgboost_metrics)
        print_row("nnue", nnue_metrics)
        print_row("consensus", consensus_metrics)

    print(f"Mean over {folds} grouped folds")
    for name, rows in totals.items():
        print_row(name, {key: float(np.mean([row[key] for row in rows])) for key in rows[0]})


def main() -> None:
    from train_nnue import load_game_move_data

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cv", type=int, default=0, help="retrain on K grouped folds")
    args = parser.parse_args()
    data = load_game_move_data(pd.read_csv(DATASET_PATH))
    if args.cv:
        compare_cross_validated(data, args.cv)
    else:
        compare_saved(data)


if __name__ == "__main__":
    main()

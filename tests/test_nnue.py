import importlib.util
import io
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import torch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
for directory in ("ml-model", "python-pipeline"):
    path = str(REPOSITORY_ROOT / directory)
    if path not in sys.path:
        sys.path.insert(0, path)


def load_module(module_name, relative_path):
    spec = importlib.util.spec_from_file_location(
        module_name, REPOSITORY_ROOT / relative_path
    )
    module = importlib.util.module_from_spec(spec)
    # Dataclasses look their module up in sys.modules while being created.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


nnue_model = load_module("nnue_model", "ml-model/nnue_model.py")

MOVE_COLUMNS = ["move_number", "complexity_score", "time_delta", "constant"]
FEATURE_SCHEMA = ["complexity_score_mean", "move_count"]


def make_moves(rng, game_lengths):
    """Random move rows whose move_number restarts at 1 for every game."""
    rows = []
    for length in game_lengths:
        for move_number in range(1, length + 1):
            rows.append([
                move_number,
                rng.integers(0, 60),
                rng.normal(0, 30),
                7.0,
            ])
    return np.asarray(rows, dtype="float32")


def make_ensemble(transition_lag=2, members=2, decision_offset=0.3):
    rng = np.random.default_rng(1)
    lengths = np.array([5, 12, 30, 1])
    moves = make_moves(rng, lengths)
    game_features = rng.normal(size=(len(lengths), len(FEATURE_SCHEMA)))
    spec = nnue_model.NNUEFeatureSpec.fit(
        moves,
        MOVE_COLUMNS,
        game_features,
        FEATURE_SCHEMA,
        bucket_count=4,
        transition_lag=transition_lag,
    )
    networks = []
    for seed in range(members):
        torch.manual_seed(seed)
        network = nnue_model.NNUENetwork(spec.sparse_size, spec.dense_size, 16, 8)
        # Spread the weights so the clipped activations are not all saturated.
        with torch.no_grad():
            network.accumulator.weight.normal_(0, 0.3)
        network.eval()
        networks.append(network)
    ensemble = nnue_model.NNUEEnsemble(spec, networks, decision_offset)
    return ensemble, moves, lengths, game_features


class FeatureSpecTests(unittest.TestCase):
    def test_constant_and_phase_columns_are_not_bucketed(self):
        ensemble, *_ = make_ensemble()
        self.assertEqual(
            ensemble.spec.bucket_columns, ["complexity_score", "time_delta"]
        )

    def test_every_move_activates_the_same_number_of_in_range_features(self):
        ensemble, moves, lengths, _ = make_ensemble()
        indices = ensemble.spec.active_indices(moves, lengths)

        self.assertEqual(indices.shape, (len(moves), ensemble.spec.features_per_move))
        self.assertTrue((indices >= 0).all())
        self.assertTrue((indices < ensemble.spec.sparse_size).all())
        # Each feature block owns a distinct index range, so no row repeats one.
        for row in indices:
            self.assertEqual(len(set(row.tolist())), len(row))

    def test_transitions_restart_at_each_game_boundary(self):
        ensemble, moves, lengths, _ = make_ensemble(transition_lag=2)
        together = ensemble.spec.active_indices(moves, lengths)
        second_game = moves[lengths[0]:lengths[0] + lengths[1]]

        # The second game scored alone must match its slice of the batch.
        np.testing.assert_array_equal(
            together[lengths[0]:lengths[0] + lengths[1]],
            ensemble.spec.active_indices(second_game),
        )

    def test_histograms_reject_games_without_moves(self):
        ensemble, moves, _, _ = make_ensemble()
        with self.assertRaises(ValueError):
            ensemble.spec.histograms(moves, np.array([len(moves), 0]))

    def test_spec_survives_a_dict_round_trip(self):
        ensemble, *_ = make_ensemble()
        restored = nnue_model.NNUEFeatureSpec.from_dict(ensemble.spec.to_dict())
        self.assertEqual(restored, ensemble.spec)


class AccumulatorTests(unittest.TestCase):
    def test_incremental_accumulator_matches_the_batched_histogram_path(self):
        for lag in (0, 2):
            with self.subTest(transition_lag=lag):
                ensemble, moves, lengths, game_features = make_ensemble(lag)
                batched = ensemble.scores(
                    ensemble.spec.histograms(moves, lengths),
                    ensemble.spec.dense_features(game_features),
                )
                starts = np.cumsum(lengths) - lengths
                for game, (start, length) in enumerate(zip(starts, lengths)):
                    accumulator = nnue_model.NNUEAccumulator(ensemble)
                    for move in moves[start:start + length]:
                        accumulator.push_move(move)
                    self.assertAlmostEqual(
                        accumulator.score(game_features[game]), batched[game], places=5
                    )

    def test_decision_offset_moves_the_half_way_point(self):
        ensemble, moves, lengths, game_features = make_ensemble(decision_offset=0.0)
        histograms = ensemble.spec.histograms(moves, lengths)
        dense = ensemble.spec.dense_features(game_features)
        raw_logits = ensemble.logits(histograms, dense)

        ensemble.decision_offset = float(raw_logits[0])
        self.assertAlmostEqual(float(ensemble.scores(histograms, dense)[0]), 0.5, places=6)

    def test_scoring_a_game_with_no_moves_is_rejected(self):
        ensemble, *_ = make_ensemble()
        with self.assertRaises(ValueError):
            nnue_model.NNUEAccumulator(ensemble).score([0.0, 0.0])


class CheckpointTests(unittest.TestCase):
    def saved_checkpoint(self, ensemble):
        buffer = io.BytesIO()
        torch.save(nnue_model.build_checkpoint(ensemble, 16, 8), buffer)
        buffer.seek(0)
        # Default torch.load is weights_only, as in the live worker.
        return torch.load(buffer, map_location="cpu")

    def test_checkpoint_round_trip_reproduces_scores(self):
        ensemble, moves, lengths, game_features = make_ensemble()
        restored = nnue_model.load_ensemble(self.saved_checkpoint(ensemble), FEATURE_SCHEMA)

        histograms = ensemble.spec.histograms(moves, lengths)
        dense = ensemble.spec.dense_features(game_features)
        np.testing.assert_allclose(
            restored.scores(histograms, dense), ensemble.scores(histograms, dense), rtol=1e-6
        )
        self.assertEqual(restored.decision_offset, ensemble.decision_offset)

    def test_checkpoint_for_another_feature_schema_is_rejected(self):
        ensemble, *_ = make_ensemble()
        checkpoint = self.saved_checkpoint(ensemble)
        with self.assertRaisesRegex(ValueError, "feature_schema_sha256"):
            nnue_model.load_ensemble(checkpoint, ["move_count", "complexity_score_mean"])

    def test_checkpoint_with_wrong_format_is_rejected(self):
        ensemble, *_ = make_ensemble()
        checkpoint = self.saved_checkpoint(ensemble)
        checkpoint["format"] = "something-else"
        with self.assertRaisesRegex(ValueError, "format"):
            nnue_model.load_ensemble(checkpoint, FEATURE_SCHEMA)


class LiveScoringTests(unittest.TestCase):
    def setUp(self):
        self.nnue_scoring = load_module("nnue_scoring", "python-pipeline/nnue_scoring.py")

    def test_align_moves_reorders_columns_and_zero_fills_missing_ones(self):
        aligned = self.nnue_scoring.align_moves(
            [[5.0, 1.0], [6.0, 2.0]],
            ["time_delta", "move_number"],
            MOVE_COLUMNS,
        )
        np.testing.assert_array_equal(
            aligned, [[1.0, 0.0, 5.0, 0.0], [2.0, 0.0, 6.0, 0.0]]
        )

    def test_align_moves_rejects_bad_input(self):
        align = self.nnue_scoring.align_moves
        for moves, columns in (
            ([], MOVE_COLUMNS),
            (None, MOVE_COLUMNS),
            ([[1.0, 2.0]], ["move_number"]),
            ([[float("nan")]], ["move_number"]),
            ([["x"]], ["move_number"]),
        ):
            with self.subTest(moves=moves), self.assertRaises(ValueError):
                align(moves, columns, MOVE_COLUMNS)

    def test_completed_game_score_matches_the_batched_model(self):
        ensemble, moves, lengths, game_features = make_ensemble()
        batched = ensemble.scores(
            ensemble.spec.histograms(moves, lengths),
            ensemble.spec.dense_features(game_features),
        )
        completed_game = {
            "game_id": "game-1",
            "features": dict(zip(FEATURE_SCHEMA, game_features[0].tolist())),
            "move_columns": MOVE_COLUMNS,
            "moves": moves[:lengths[0]].tolist(),
        }

        score = self.nnue_scoring.score_completed_game(
            ensemble, completed_game, FEATURE_SCHEMA
        )
        self.assertAlmostEqual(score, float(batched[0]), places=5)

    def test_completed_game_without_moves_is_rejected(self):
        ensemble, *_ = make_ensemble()
        with self.assertRaisesRegex(ValueError, "moves"):
            self.nnue_scoring.score_completed_game(
                ensemble, {"game_id": "g", "features": {}}, FEATURE_SCHEMA
            )


class TrainingTests(unittest.TestCase):
    def setUp(self):
        self.training_data = load_module("training_data", "ml-model/training_data.py")
        self.train_nnue = load_module("train_nnue", "ml-model/train_nnue.py")

    def synthetic_dataframe(self, games=24):
        rng = np.random.default_rng(3)
        rows = []
        for game in range(games):
            cheating = game % 2
            for move_number in rng.permutation(np.arange(1, 9)):
                rows.append({
                    "GameID": f"game-{game}",
                    "move_number": int(move_number),
                    "complexity_score": float(rng.integers(0, 50) + 20 * cheating),
                    "time_delta": float(rng.normal(0, 5)),
                    "Cheating": cheating,
                })
        return pd.DataFrame(rows)

    def test_move_sequences_follow_game_order_then_move_number(self):
        dataframe = self.synthetic_dataframe(games=3)
        order = pd.Index(["game-2", "game-0", "game-1"])
        moves, lengths, columns = self.training_data.prepare_game_move_sequences(
            dataframe, order
        )

        self.assertEqual(columns, ["move_number", "complexity_score", "time_delta"])
        np.testing.assert_array_equal(lengths, [8, 8, 8])
        first_game = dataframe[dataframe["GameID"] == "game-2"].sort_values("move_number")
        np.testing.assert_array_equal(moves[:8], first_game[columns].to_numpy("float32"))

    def test_move_sequences_reject_moves_from_unknown_games(self):
        dataframe = self.synthetic_dataframe(games=2)
        with self.assertRaises(ValueError):
            self.training_data.prepare_game_move_sequences(dataframe, pd.Index(["game-0"]))

    def test_fit_nnue_produces_a_working_ensemble_and_threshold(self):
        data = self.train_nnue.load_game_move_data(self.synthetic_dataframe())
        config = self.train_nnue.NNUETrainingConfig(
            seeds=1, max_epochs=3, patience=1, validation_folds=2, bucket_count=4
        )
        train_index = np.arange(16)
        ensemble, info = self.train_nnue.fit_nnue(data, train_index, config, log=lambda _: None)

        scores = self.train_nnue.score_games(ensemble, data, np.arange(16, 24))
        self.assertEqual(scores.shape, (8,))
        self.assertTrue(((scores >= 0) & (scores <= 1)).all())
        self.assertEqual(info["threshold"], 0.55)
        self.assertAlmostEqual(
            ensemble.decision_offset,
            np.log(info["threshold"] / (1 - info["threshold"])),
        )

    def test_threshold_prefers_lower_fpr_when_accuracy_is_tied(self):
        # Raising the cutoff to 0.6 trades one true positive for one fewer
        # false positive: identical accuracy, lower false positive rate.
        scores = [0.55, 0.9, 0.58, 0.2, 0.1, 0.95, 0.3]
        labels = [0, 1, 1, 0, 0, 1, 0]
        chosen = self.train_nnue.choose_threshold(scores, labels, (0.5, 0.6))
        self.assertEqual(chosen, 0.6)

    def test_threshold_rejects_a_clear_accuracy_loss(self):
        rng = np.random.default_rng(0)
        labels = rng.integers(0, 2, 4000)
        # Positives sit in [0.5, 0.7); a 0.7 cutoff would miss all of them.
        scores = np.where(labels == 1, rng.uniform(0.5, 0.7, 4000), rng.uniform(0, 0.5, 4000))
        chosen = self.train_nnue.choose_threshold(scores, labels, (0.5, 0.7))
        self.assertEqual(chosen, 0.5)

    def test_fit_nnue_can_choose_its_threshold_from_out_of_fold_scores(self):
        data = self.train_nnue.load_game_move_data(self.synthetic_dataframe())
        config = self.train_nnue.NNUETrainingConfig(
            seeds=1, max_epochs=3, patience=1, validation_folds=2, bucket_count=4,
            fixed_threshold=None,
        )
        _, info = self.train_nnue.fit_nnue(data, np.arange(16), config, log=lambda _: None)
        self.assertIn(info["threshold"], config.threshold_grid)

    def test_classification_metrics_counts_false_positives_against_negatives(self):
        metrics = self.train_nnue.classification_metrics(
            [0.9, 0.2, 0.7, 0.1], [0, 0, 1, 1]
        )
        self.assertEqual(metrics["accuracy"], 0.5)
        self.assertEqual(metrics["false_positive_rate"], 0.5)
        self.assertEqual(metrics["true_positive_rate"], 0.5)


class EngineSwitchTests(unittest.TestCase):
    def setUp(self):
        self.engine_config = load_module("engine_config", "python-pipeline/engine_config.py")

    def test_engine_defaults_to_xgboost_and_accepts_nnue(self):
        get = self.engine_config.get_selected_engine
        self.assertEqual(get({}), "xgboost")
        self.assertEqual(get({"CHESS_ENGINE": "  "}), "xgboost")
        self.assertEqual(get({"CHESS_ENGINE": " NNUE "}), "nnue")

    def test_unknown_engine_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "CHESS_ENGINE"):
            self.engine_config.get_selected_engine({"CHESS_ENGINE": "stockfish"})

    def test_pipeline_launches_the_selected_engine_worker(self):
        live_pipeline = load_module("live_pipeline_engine", "python-pipeline/live_pipeline.py")
        commands = live_pipeline.worker_commands("/py", Path("/repo"), engine="nnue")
        self.assertEqual(
            [command[1] for command in commands],
            [
                "/repo/live_game_collector.py",
                "/repo/nnue_inference.py",
                "/repo/torch_inference.py",
                "/repo/prediction_reporter.py",
            ],
        )

    def test_reporter_joins_pytorch_with_the_nnue_engine(self):
        reporting = load_module("prediction_reporting_engine", "python-pipeline/prediction_reporting.py")
        reporter = reporting.PredictionReporter(engine="nnue")

        with self.assertRaises(ValueError):
            reporter.add_prediction({"game_id": "g Cheating", "model": "xgboost", "score": 0.2})
        self.assertIsNone(
            reporter.add_prediction({"game_id": "g Cheating", "model": "nnue", "score": 0.8})
        )
        result = reporter.add_prediction(
            {"game_id": "g Cheating", "model": "pytorch", "score": 0.3}
        )

        self.assertEqual(result["engine"], "nnue")
        self.assertEqual(result["xgboost_score"], 0.8)
        self.assertEqual(result["xgboost_prediction"], 1)
        self.assertEqual(result["xgboost_accuracy"], 1.0)
        self.assertEqual(result["pytorch_accuracy"], 0.0)

    def test_consensus_engine_is_selectable(self):
        get = self.engine_config.get_selected_engine
        self.assertEqual(get({"CHESS_ENGINE": "Consensus"}), "consensus")

    def test_consensus_score_crosses_half_only_when_both_engines_do(self):
        combine = self.engine_config.combine_engine_scores
        for xgboost_score, nnue_score, flagged in [
            (0.9, 0.8, True),
            (0.5, 0.5, True),
            (0.9, 0.49, False),
            (0.2, 0.95, False),
            (0.1, 0.3, False),
        ]:
            score = combine("consensus", {"xgboost": xgboost_score, "nnue": nnue_score})
            self.assertEqual(score >= 0.5, flagged, (xgboost_score, nnue_score))
            self.assertTrue(0.0 <= score <= 1.0)
        self.assertEqual(combine("nnue", {"nnue": 0.7, "xgboost": 0.1}), 0.7)

    def test_pipeline_launches_both_engine_workers_for_consensus(self):
        live_pipeline = load_module("live_pipeline_consensus", "python-pipeline/live_pipeline.py")
        commands = live_pipeline.worker_commands("/py", Path("/repo"), engine="consensus")
        self.assertEqual(
            [command[1] for command in commands],
            [
                "/repo/live_game_collector.py",
                "/repo/xgboost_inference.py",
                "/repo/nnue_inference.py",
                "/repo/torch_inference.py",
                "/repo/prediction_reporter.py",
            ],
        )

    def test_consensus_reporter_waits_for_all_three_models(self):
        reporting = load_module("prediction_reporting_consensus", "python-pipeline/prediction_reporting.py")
        reporter = reporting.PredictionReporter(engine="consensus")

        self.assertIsNone(
            reporter.add_prediction({"game_id": "g Clean", "model": "xgboost", "score": 0.9})
        )
        self.assertIsNone(
            reporter.add_prediction({"game_id": "g Clean", "model": "pytorch", "score": 0.1})
        )
        result = reporter.add_prediction({"game_id": "g Clean", "model": "nnue", "score": 0.3})

        # XGBoost alone would flag this clean game; NNUE disagrees, so consensus does not.
        self.assertEqual(result["engine"], "consensus")
        self.assertEqual(result["engine_scores"], {"xgboost": 0.9, "nnue": 0.3})
        self.assertEqual(result["xgboost_score"], 0.3)
        self.assertEqual(result["xgboost_prediction"], 0)
        self.assertEqual(result["xgboost_accuracy"], 1.0)

        reporter.add_prediction({"game_id": "h Cheating", "model": "xgboost", "score": 0.7})
        reporter.add_prediction({"game_id": "h Cheating", "model": "nnue", "score": 0.6})
        result = reporter.add_prediction({"game_id": "h Cheating", "model": "pytorch", "score": 0.9})
        self.assertEqual(result["xgboost_prediction"], 1)
        self.assertAlmostEqual(result["xgboost_score"], 0.6)

    def test_collector_publishes_ordered_moves_for_the_nnue_engine(self):
        live_scoring = load_module("live_scoring_engine", "python-pipeline/live_scoring.py")
        collector = live_scoring.GameFeatureCollector(["move_count"])
        collector.add_feature({"game_id": "g", "move_number": 2, "time_delta": 4.0, "move": "e5"})
        collector.add_feature({"game_id": "g", "move_number": 1, "time_delta": 3.0, "move": "e4"})

        completed_game = collector.prepare_completed_game("g")
        self.assertEqual(completed_game["move_columns"], ["move_number", "time_delta"])
        self.assertEqual(completed_game["moves"], [[1.0, 3.0], [2.0, 4.0]])
        self.assertEqual(completed_game["features"], {"move_count": 2.0})


if __name__ == "__main__":
    unittest.main()

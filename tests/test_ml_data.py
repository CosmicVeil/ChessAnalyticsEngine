import csv
import importlib.util
import os
import sys
import tempfile
import unittest
import warnings
from unittest.mock import patch
from pathlib import Path

import pandas as pd


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def load_module(module_name, relative_path):
    module_path = REPOSITORY_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TrainingDataTests(unittest.TestCase):
    def test_launcher_runs_each_selected_model_in_a_separate_process(self):
        sys.path.insert(0, str(REPOSITORY_ROOT / "ml-model"))
        previous_directory = Path.cwd()
        try:
            os.chdir(REPOSITORY_ROOT / "ml-model")
            with patch("builtins.input", return_value="0"):
                launcher = load_module("train_launcher", "ml-model/train.py")
        finally:
            os.chdir(previous_directory)
            sys.path.pop(0)

        with patch.object(launcher.subprocess, "run") as run:
            launcher.launch_models([1, 2])

        self.assertEqual(run.call_count, 2)
        launched_scripts = [call.args[0][1] for call in run.call_args_list]
        self.assertEqual(launched_scripts, [
            str(REPOSITORY_ROOT / "ml-model" / "train_torch.py"),
            str(REPOSITORY_ROOT / "ml-model" / "train_xgboost.py"),
        ])

    def test_collector_writes_one_complete_row_per_message(self):
        csv_dataset = load_module("csv_dataset", "python-pipeline/csv_dataset.py")

        with tempfile.TemporaryDirectory() as temporary_directory:
            csv_path = Path(temporary_directory) / "chess_dataset.csv"
            csv_dataset.append_labeled_record(
                csv_path,
                {"move": "e4", "move_number": 1, "time": 600},
                "Alice vs. Bob Clean",
            )
            csv_dataset.append_labeled_record(
                csv_path,
                {"move": "e5", "move_number": 1, "time": 600},
                "Carol vs. Dan Cheating",
            )

            with csv_path.open(newline="") as csv_file:
                rows = list(csv.DictReader(csv_file))

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["GameID"], "Alice vs. Bob Clean")
        self.assertEqual(rows[0]["move"], "e4")
        self.assertEqual(rows[0]["Cheating"], "0")
        self.assertEqual(rows[1]["Cheating"], "1")

    def test_completion_event_is_not_stored_as_a_training_move(self):
        csv_dataset = load_module("csv_dataset", "python-pipeline/csv_dataset.py")

        self.assertTrue(csv_dataset.should_store_feature_event({"move_number": 12}))
        self.assertFalse(csv_dataset.should_store_feature_event({"game_complete": True}))

    def test_preparation_excludes_game_ids_and_encodes_remaining_features(self):
        training_data = load_module("training_data", "ml-model/training_data.py")
        dataframe = pd.DataFrame(
            {
                "GameID": ["Alice vs. Bob Clean", "Carol vs. Dan Cheating"],
                "move": ["e4", "e5"],
                "move_number": [1, 1],
                "time": [600, 600],
                "Cheating": [0, 1],
            }
        )

        with warnings.catch_warnings():
            warnings.simplefilter("error", pd.errors.Pandas4Warning)
            features, labels = training_data.prepare_training_data(dataframe)

        self.assertEqual(labels.tolist(), [0.0, 1.0])
        self.assertEqual(features.shape[1], 4)
        self.assertNotIn("GameID", features.columns)
        self.assertFalse(any(column.startswith("GameID_") for column in features.columns))
        self.assertIn("move_e4", features.columns)
        self.assertIn("move_e5", features.columns)
        self.assertNotIn("Cheating", features.columns)

    def test_grouped_split_keeps_every_game_id_in_exactly_one_partition(self):
        training_data = load_module("training_data", "ml-model/training_data.py")
        dataframe = pd.DataFrame(
            {
                "GameID": ["game-a", "game-a", "game-b", "game-b", "game-c", "game-c"],
                "move_number": [1, 2, 1, 2, 1, 2],
                "Cheating": [0, 0, 1, 1, 0, 0],
            }
        )
        features, labels = training_data.prepare_training_data(dataframe)
        groups = training_data.get_game_ids(dataframe)

        train_index, test_index = training_data.group_train_test_indices(
            features, labels, groups, train_size=0.5, random_state=42
        )

        train_groups = set(groups.iloc[train_index])
        test_groups = set(groups.iloc[test_index])
        self.assertTrue(train_groups.isdisjoint(test_groups))
        self.assertEqual(train_groups | test_groups, set(groups))
        self.assertFalse(any(column.lower().replace("_", "") == "gameid" for column in features.columns))

    def test_game_preparation_aggregates_moves_into_one_numeric_row_per_game(self):
        training_data = load_module("training_data", "ml-model/training_data.py")
        dataframe = pd.DataFrame(
            {
                "GameID": ["clean-1", "clean-1", "cheating-1", "cheating-1"],
                "move_number": [1, 2, 1, 2],
                "material_balance": [0, 1, 0, 3],
                "move": ["e4", "e5", "d4", "d5"],
                "Cheating": [0, 0, 1, 1],
            }
        )

        features, labels = training_data.prepare_game_training_data(dataframe)

        self.assertEqual(len(features), 2)
        self.assertEqual(features.loc["clean-1", "move_number_mean"], 1.5)
        self.assertEqual(features.loc["clean-1", "move_count"], 2.0)
        self.assertEqual(labels.loc["cheating-1"], 1.0)
        self.assertFalse(any("gameid" in column.lower() for column in features.columns))
        self.assertNotIn("move", features.columns)

    def test_game_preparation_rejects_conflicting_game_labels(self):
        training_data = load_module("training_data", "ml-model/training_data.py")
        dataframe = pd.DataFrame(
            {
                "GameID": ["game-1", "game-1"],
                "move_number": [1, 2],
                "Cheating": [0, 1],
            }
        )

        with self.assertRaisesRegex(ValueError, "one Cheating label"):
            training_data.prepare_game_training_data(dataframe)

    def test_feature_schema_preserves_model_column_order(self):
        model_artifacts = load_module("model_artifacts", "ml-model/model_artifacts.py")

        with tempfile.TemporaryDirectory() as temporary_directory:
            model_directory = Path(temporary_directory)
            saved_path = model_artifacts.save_feature_schema(
                ["move_count", "time_delta_mean"], model_directory
            )

            self.assertEqual(saved_path.name, "feature_schema.json")
            self.assertEqual(
                model_artifacts.load_feature_schema(model_directory),
                ["move_count", "time_delta_mean"],
            )

    def test_model_artifact_path_uses_the_models_directory_not_the_schema_file(self):
        model_artifacts = load_module("model_artifacts", "ml-model/model_artifacts.py")

        with tempfile.TemporaryDirectory() as temporary_directory:
            model_directory = Path(temporary_directory)
            schema_path = model_artifacts.save_feature_schema(["move_count"], model_directory)
            artifact_path = model_artifacts.model_artifact_path(
                "pytorch_model.pt", model_directory
            )

            self.assertEqual(schema_path, model_directory / "feature_schema.json")
            self.assertEqual(artifact_path, model_directory / "pytorch_model.pt")

    def test_live_scoring_waits_for_completion_and_tracks_model_accuracy(self):
        live_scoring = load_module("live_scoring", "python-pipeline/live_scoring.py")
        tracker = live_scoring.GameScoreTracker(
            feature_schema=["move_number_mean", "move_count"],
            pytorch_predict=lambda row: 0.8,
            xgboost_predict=lambda row: 0.2,
        )

        tracker.add_feature({"game_id": "game-1 Cheating", "move_number": 1})
        tracker.add_feature({"game_id": "game-1 Cheating", "move_number": 2})

        self.assertIsNone(tracker.complete_game("missing-game"))
        result = tracker.complete_game("game-1 Cheating")

        self.assertEqual(result["pytorch_score"], 0.8)
        self.assertEqual(result["xgboost_score"], 0.2)
        self.assertEqual(result["pytorch_accuracy"], 1.0)
        self.assertEqual(result["xgboost_accuracy"], 0.0)

    def test_live_scoring_fills_missing_schema_features_with_zero(self):
        live_scoring = load_module("live_scoring", "python-pipeline/live_scoring.py")
        model_rows = []

        def capture_row(row):
            model_rows.append(row)
            return 0.0

        tracker = live_scoring.GameScoreTracker(
            feature_schema=["material_balance_mean", "move_count"],
            pytorch_predict=capture_row,
            xgboost_predict=lambda row: 0.0,
        )
        tracker.add_feature({"game_id": "game-2 Clean", "move_number": 1})

        tracker.complete_game("game-2 Clean")

        self.assertEqual(model_rows[0].loc["game-2 Clean", "material_balance_mean"], 0.0)

    def test_game_feature_collector_emits_one_ordered_row_after_completion(self):
        live_scoring = load_module("live_scoring", "python-pipeline/live_scoring.py")
        collector = live_scoring.GameFeatureCollector(
            ["move_number_mean", "material_balance_last", "move_count"]
        )
        collector.add_feature(
            {"game_id": "game-3 Clean", "move_number": 1, "material_balance": 0}
        )
        collector.add_feature(
            {"game_id": "game-3 Clean", "move_number": 2, "material_balance": 2}
        )

        result = collector.complete_game("game-3 Clean")

        self.assertEqual(result["game_id"], "game-3 Clean")
        self.assertEqual(result["features"], {
            "move_number_mean": 1.5,
            "material_balance_last": 2.0,
            "move_count": 2.0,
        })
        self.assertIsNone(collector.complete_game("game-3 Clean"))

    def test_prediction_reporter_waits_for_both_model_scores(self):
        prediction_reporting = load_module(
            "prediction_reporting", "python-pipeline/prediction_reporting.py"
        )
        reporter = prediction_reporting.PredictionReporter()

        self.assertIsNone(reporter.add_prediction({
            "game_id": "game-4 Cheating", "model": "pytorch", "score": 0.9
        }))
        result = reporter.add_prediction({
            "game_id": "game-4 Cheating", "model": "xgboost", "score": 0.1
        })

        self.assertEqual(result["pytorch_prediction"], 1)
        self.assertEqual(result["xgboost_prediction"], 0)
        self.assertEqual(result["pytorch_accuracy"], 1.0)
        self.assertEqual(result["xgboost_accuracy"], 0.0)

    def test_live_entry_scripts_import_without_a_pipeline_working_directory(self):
        load_module("live_game_collector", "python-pipeline/live_game_collector.py")
        load_module("prediction_reporter", "python-pipeline/prediction_reporter.py")


if __name__ == "__main__":
    unittest.main()

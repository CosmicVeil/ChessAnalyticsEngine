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


if __name__ == "__main__":
    unittest.main()

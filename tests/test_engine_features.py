import json
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MODEL_DIRECTORY = str(REPOSITORY_ROOT / "ml-model")
if MODEL_DIRECTORY not in sys.path:
    sys.path.insert(0, MODEL_DIRECTORY)

import add_engine_features  # noqa: E402
import engine_features  # noqa: E402


class EngineFeatureRuleTests(unittest.TestCase):
    def test_clamp_maps_mates_and_large_scores_to_the_limit(self):
        clamp = engine_features.clamp_centipawns
        self.assertEqual(clamp(120, None), 120)
        self.assertEqual(clamp(5000, None), 1000)
        self.assertEqual(clamp(-5000, None), -1000)
        self.assertEqual(clamp(None, 3), 1000)
        self.assertEqual(clamp(None, -2), -1000)
        self.assertEqual(clamp(None, None), 0)

    def test_checkmate_counts_against_the_side_to_move(self):
        self.assertEqual(engine_features.checkmate_evaluation(white_to_move=True), -1000)
        self.assertEqual(engine_features.checkmate_evaluation(white_to_move=False), 1000)

    def test_centipawn_loss_is_measured_from_the_movers_side_and_never_negative(self):
        loss = engine_features.centipawn_loss
        self.assertEqual(loss(50, -20, white_moved=True), 70)
        self.assertEqual(loss(50, 110, white_moved=False), 60)
        self.assertEqual(loss(50, 110, white_moved=True), 0)

    def test_win_chance_is_centred_and_symmetric(self):
        win = engine_features.win_chance
        self.assertAlmostEqual(win(0), 50.0)
        self.assertAlmostEqual(win(300) + win(-300), 100.0)
        self.assertGreater(win(1000), 97)

    def test_move_features_combine_the_four_columns(self):
        row = engine_features.move_features(before=40, after=-60, after_mate=None, white_moved=True)
        self.assertEqual(row["centipawn_loss"], 100)
        self.assertEqual(row["evaluation"], -60)
        self.assertEqual(row["mate"], 0)
        self.assertAlmostEqual(row["win_chance"], engine_features.win_chance(-60))


class DatasetToolTests(unittest.TestCase):
    def dataframe(self):
        rows = []
        for game_id, moves in (("A vs. B #7 Clean", 3), ("C vs. D #2 Cheating", 2)):
            for move_number in range(1, moves + 1):
                rows.append({
                    "game_id": game_id,
                    "move_number": move_number,
                    "GameID": game_id,
                    "Cheating": int("Cheating" in game_id),
                    "centipawn_loss": 0,
                    "evaluation": 0,
                    "win_chance": 0,
                    "mate": 0,
                })
        return pd.DataFrame(rows)

    def test_wanted_games_maps_ids_to_source_positions_and_move_counts(self):
        wanted = add_engine_features.wanted_games(self.dataframe())
        self.assertEqual(wanted["Clean"], {7: ("A vs. B #7 Clean", 3)})
        self.assertEqual(wanted["Cheating"], {2: ("C vs. D #2 Cheating", 2)})

    def test_merge_fills_columns_by_move_and_backs_up_the_original(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            originals = (
                add_engine_features.DATASET,
                add_engine_features.BACKUP,
                add_engine_features.CACHE,
            )
            add_engine_features.DATASET = directory / "chess_dataset.csv"
            add_engine_features.BACKUP = directory / "backup.csv"
            add_engine_features.CACHE = directory / "cache.jsonl"
            try:
                dataframe = self.dataframe()
                dataframe.to_csv(add_engine_features.DATASET, index=False)
                cache_rows = {
                    "A vs. B #7 Clean": [
                        {"centipawn_loss": k, "evaluation": 10 * k, "win_chance": 50.0 + k, "mate": 0}
                        for k in (1, 2, 3)
                    ],
                }
                add_engine_features.CACHE.write_text(
                    "".join(
                        json.dumps({"game_id": game_id, "moves": moves}) + "\n"
                        for game_id, moves in cache_rows.items()
                    )
                )

                add_engine_features.merge(dataframe)

                merged = pd.read_csv(add_engine_features.DATASET)
                self.assertTrue(add_engine_features.BACKUP.exists())
                # The game without analysis is dropped rather than left at zero.
                self.assertEqual(set(merged["GameID"]), {"A vs. B #7 Clean"})
                self.assertEqual(merged["centipawn_loss"].tolist(), [1, 2, 3])
                self.assertEqual(merged["evaluation"].tolist(), [10, 20, 30])
            finally:
                (
                    add_engine_features.DATASET,
                    add_engine_features.BACKUP,
                    add_engine_features.CACHE,
                ) = originals


if __name__ == "__main__":
    unittest.main()

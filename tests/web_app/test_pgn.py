"""Contracts for streaming PGN parsing and move-event conversion."""

import unittest
import uuid
from pathlib import Path

import chess.pgn

from webapp.domain import InvalidGame
from webapp.pgn import build_move_events, iter_pgn_games, pipeline_game_id


FIXTURE_DIRECTORY = Path(__file__).resolve().parents[1] / "fixtures" / "webapp"
SMALL_BATCH = FIXTURE_DIRECTORY / "small_batch.pgn"
MALFORMED_THEN_VALID = FIXTURE_DIRECTORY / "malformed_then_valid.pgn"
JOB_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")


def read_games(path: Path):
    with path.open(encoding="utf-8") as stream:
        return list(iter_pgn_games(stream))


class PipelineGameIdTests(unittest.TestCase):
    def test_pipeline_game_id_is_stable_and_one_based(self):
        self.assertEqual(
            pipeline_game_id(JOB_ID, 1),
            "web:12345678-1234-5678-1234-567812345678:1",
        )
        self.assertEqual(
            pipeline_game_id(JOB_ID, 2),
            "web:12345678-1234-5678-1234-567812345678:2",
        )
        self.assertEqual(
            pipeline_game_id(JOB_ID, 1),
            pipeline_game_id(JOB_ID, 1),
        )

        with self.assertRaises(ValueError):
            pipeline_game_id(JOB_ID, 0)


class PgnParsingTests(unittest.TestCase):
    def test_iterator_yields_before_reading_the_entire_source(self):
        with SMALL_BATCH.open(encoding="utf-8") as stream:
            iterator = iter_pgn_games(stream)

            first = next(iterator)

            self.assertNotIsInstance(first, InvalidGame)
            self.assertEqual(first.sequence, 1)
            self.assertIsInstance(first.game, chess.pgn.Game)
            self.assertEqual(len(list(first.game.mainline_moves())), 4)
            self.assertLess(stream.tell(), SMALL_BATCH.stat().st_size)

    def test_small_batch_preserves_metadata_and_rejects_empty_mainline(self):
        games = read_games(SMALL_BATCH)

        self.assertEqual([game.sequence for game in games], [1, 2, 3, 4])
        self.assertNotIsInstance(games[0], InvalidGame)
        self.assertEqual(games[0].white_name, "Ada")
        self.assertEqual(games[0].black_name, "Grace")
        self.assertEqual(games[0].white_elo, 1500)
        self.assertEqual(games[0].black_elo, 1450)
        self.assertEqual(games[0].result, "0-1")
        self.assertEqual(games[0].opening, "Fool's Mate")

        self.assertNotIsInstance(games[1], InvalidGame)
        self.assertIsNone(games[1].white_elo)
        self.assertIsNone(games[1].black_elo)

        self.assertIsInstance(games[3], InvalidGame)
        self.assertTrue(games[3].error_code)
        self.assertTrue(games[3].error_message)

    def test_malformed_game_is_reported_before_later_valid_game(self):
        games = read_games(MALFORMED_THEN_VALID)

        self.assertEqual(len(games), 2)
        self.assertIsInstance(games[0], InvalidGame)
        self.assertEqual(games[0].sequence, 1)
        self.assertTrue(games[0].error_code)
        self.assertIn("Qxa1", games[0].error_message)

        recovered = games[1]
        self.assertNotIsInstance(recovered, InvalidGame)
        self.assertEqual(recovered.sequence, 2)
        self.assertEqual(recovered.white_name, "Valid")
        self.assertEqual(
            [event["move"] for event in build_move_events(
                recovered,
                pipeline_game_id(JOB_ID, recovered.sequence),
            )],
            ["c4", "e5", "Nc3", "Nf6"],
        )


class MoveEventTests(unittest.TestCase):
    def setUp(self):
        games = read_games(SMALL_BATCH)
        self.checkmate, self.resignation, self.draw = games[:3]

    def events_for(self, game):
        game_id = pipeline_game_id(JOB_ID, game.sequence)
        return list(build_move_events(game, game_id))

    def test_events_preserve_san_ply_order_and_wire_shape(self):
        events = self.events_for(self.checkmate)

        self.assertEqual(
            events[0],
            {
                "game_id": "web:12345678-1234-5678-1234-567812345678:1",
                "move": "f3",
                "move_number": 1,
                "White Rating": "1500",
                "Black Rating": "1450",
                "time": 600.0,
                "game_complete": False,
            },
        )
        self.assertEqual(
            [event["move"] for event in events],
            ["f3", "e5", "g4", "Qh4#"],
        )
        self.assertEqual(
            [event["move_number"] for event in events],
            [1, 2, 3, 4],
        )
        self.assertTrue(all(isinstance(event["White Rating"], str) for event in events))
        self.assertTrue(all(isinstance(event["Black Rating"], str) for event in events))

    def test_missing_ratings_and_clocks_use_wire_safe_defaults(self):
        events = self.events_for(self.resignation)

        self.assertEqual(
            [event["White Rating"] for event in events],
            ["0", "0", "0", "0", "0", "0"],
        )
        self.assertEqual(
            [event["Black Rating"] for event in events],
            ["0", "0", "0", "0", "0", "0"],
        )
        self.assertEqual(
            [event["time"] for event in events],
            [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        )

        draw_events = self.events_for(self.draw)
        self.assertEqual(
            [event["time"] for event in draw_events],
            [300.0, 0.0, 0.0, 0.0],
        )

    def test_only_final_ply_completes_checkmate_resignation_and_draw(self):
        cases = {
            "checkmate": self.checkmate,
            "resignation": self.resignation,
            "agreed draw": self.draw,
        }

        for case_name, game in cases.items():
            with self.subTest(case=case_name):
                events = self.events_for(game)
                self.assertGreater(len(events), 0)
                self.assertEqual(
                    [event["game_complete"] for event in events],
                    [False] * (len(events) - 1) + [True],
                )


if __name__ == "__main__":
    unittest.main()

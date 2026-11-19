"""Per-job engine metadata survives aggregation and reporting."""

import importlib.util
import sys
import unittest
from pathlib import Path


PIPELINE = Path(__file__).resolve().parents[1] / "python-pipeline"
if str(PIPELINE) not in sys.path:
    sys.path.insert(0, str(PIPELINE))


def load(name):
    spec = importlib.util.spec_from_file_location(name, PIPELINE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PerJobEngineTests(unittest.TestCase):
    def test_collector_keeps_engine_choice_on_completed_game(self):
        scoring = load("live_scoring")
        collector = scoring.GameFeatureCollector(["move_count"])
        collector.add_feature({"game_id": "web:job:1", "move_number": 1, "engine": "nnue"})

        completed = collector.prepare_completed_game("web:job:1")

        self.assertEqual(completed["engine"], "nnue")

    def test_reporter_uses_each_predictions_engine_in_one_process(self):
        reporting = load("prediction_reporting")
        reporter = reporting.PredictionReporter(engine="xgboost")

        self.assertIsNone(reporter.add_prediction({"game_id": "a", "model": "pytorch", "score": .2, "engine": "nnue"}))
        nnue = reporter.add_prediction({"game_id": "a", "model": "nnue", "score": .8, "engine": "nnue"})
        self.assertEqual(nnue["engine"], "nnue")
        self.assertAlmostEqual(nnue["xgboost_score"], .8)

        reporter.add_prediction({"game_id": "b", "model": "pytorch", "score": .2, "engine": "consensus"})
        reporter.add_prediction({"game_id": "b", "model": "xgboost", "score": .9, "engine": "consensus"})
        consensus = reporter.add_prediction({"game_id": "b", "model": "nnue", "score": .4, "engine": "consensus"})
        self.assertEqual(consensus["engine"], "consensus")
        self.assertAlmostEqual(consensus["xgboost_score"], .4)


if __name__ == "__main__":
    unittest.main()

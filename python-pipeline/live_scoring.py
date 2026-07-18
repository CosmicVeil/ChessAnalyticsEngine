"""Game-level scoring logic shared by the Kafka live inference consumer."""

from collections import defaultdict
from pathlib import Path
import sys
from typing import Callable

import pandas as pd


MODEL_DIRECTORY = Path(__file__).resolve().parents[1] / "ml-model"
if str(MODEL_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(MODEL_DIRECTORY))

from training_data import aggregate_game_features


PredictFunction = Callable[[pd.DataFrame], float]


class GameFeatureCollector:
    """Turn a stream of move features into one ordered feature row per game."""

    def __init__(self, feature_schema: list[str]) -> None:
        self.feature_schema = feature_schema
        self.records: dict[str, list[dict]] = defaultdict(list)

    def add_feature(self, event: dict) -> None:
        """Keep one move's features until its game has finished."""
        game_id = event.get("game_id")
        if not game_id:
            raise ValueError("Feature event must contain a game_id.")
        move_number = event.get("move_number")
        if move_number is not None and any(
            record.get("move_number") == move_number for record in self.records[game_id]
        ):
            return
        self.records[game_id].append(event)

    def prepare_completed_game(self, game_id: str) -> dict | None:
        """Build a completed-game vector without discarding retryable move state."""
        records = self.records.get(game_id)
        if not records:
            return None

        # This is the move-to-game step: many move records become one game vector.
        game_features = aggregate_game_features(pd.DataFrame(records)).reindex(
            columns=self.feature_schema, fill_value=0.0
        )
        return {
            "game_id": game_id,
            "features": {
                column: float(value)
                for column, value in game_features.iloc[0].items()
            },
        }

    def discard_game(self, game_id: str) -> None:
        """Remove state only after its completed-game event was delivered."""
        self.records.pop(game_id, None)

    def complete_game(self, game_id: str) -> dict | None:
        """Aggregate and discard a finished game for callers with no delivery step."""
        completed_game = self.prepare_completed_game(game_id)
        if completed_game is not None:
            self.discard_game(game_id)
        return completed_game


class GameScoreTracker:
    """Accumulate feature events and produce one prediction for each game."""

    def __init__(
        self,
        feature_schema: list[str],
        pytorch_predict: PredictFunction,
        xgboost_predict: PredictFunction,
    ) -> None:
        self.feature_schema = feature_schema
        self.pytorch_predict = pytorch_predict
        self.xgboost_predict = xgboost_predict
        self.records: dict[str, list[dict]] = defaultdict(list)
        self.correct_predictions = {"pytorch": 0, "xgboost": 0}
        self.labelled_games = 0

    def add_feature(self, event: dict) -> None:
        """Store one non-final move feature event under its game ID."""
        game_id = event.get("game_id")
        if not game_id:
            raise ValueError("Feature event must contain a game_id.")
        self.records[game_id].append(event)

    def complete_game(self, game_id: str) -> dict | None:
        """Score and discard one complete game, or ignore an unknown completion."""
        records = self.records.pop(game_id, None)
        if not records:
            return None

        features = aggregate_game_features(pd.DataFrame(records)).reindex(
            columns=self.feature_schema, fill_value=0.0
        )
        pytorch_score = float(self.pytorch_predict(features))
        xgboost_score = float(self.xgboost_predict(features))
        pytorch_prediction = int(pytorch_score >= 0.5)
        xgboost_prediction = int(xgboost_score >= 0.5)
        label = self._simulator_label(game_id)

        if label is not None:
            self.labelled_games += 1
            self.correct_predictions["pytorch"] += int(pytorch_prediction == label)
            self.correct_predictions["xgboost"] += int(xgboost_prediction == label)

        return {
            "game_id": game_id,
            "pytorch_score": pytorch_score,
            "xgboost_score": xgboost_score,
            "pytorch_prediction": pytorch_prediction,
            "xgboost_prediction": xgboost_prediction,
            "pytorch_accuracy": self._accuracy("pytorch"),
            "xgboost_accuracy": self._accuracy("xgboost"),
        }

    def _accuracy(self, model_name: str) -> float | None:
        if self.labelled_games == 0:
            return None
        return self.correct_predictions[model_name] / self.labelled_games

    @staticmethod
    def _simulator_label(game_id: str) -> int | None:
        lowered_game_id = game_id.lower()
        if "cheating" in lowered_game_id:
            return 1
        if "clean" in lowered_game_id:
            return 0
        return None

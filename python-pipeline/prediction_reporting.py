"""Join isolated model predictions into one game-level result."""

from pathlib import Path
import sys


PIPELINE_DIRECTORY = Path(__file__).resolve().parent
if str(PIPELINE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIRECTORY))

from engine_config import ENGINE_MODELS, combine_engine_scores, get_selected_engine


class PredictionReporter:
    """Wait for both model workers before reporting a completed game's result.

    PyTorch is always one side. The other side is the selected engine
    (XGBoost, NNUE, or the XGBoost-and-NNUE consensus). Its result keeps the
    ``xgboost_*`` field names, which the web result worker and database
    already store. The ``engine`` field records which engine actually
    produced those values, and ``engine_scores`` holds each component score.
    """

    def __init__(self, engine: str | None = None) -> None:
        self.engine = get_selected_engine() if engine is None else engine
        self.engine_models = ENGINE_MODELS[self.engine]
        self.model_names = {"pytorch", *self.engine_models}
        self.pending_scores: dict[str, dict[str, float]] = {}
        self.correct_predictions = {"pytorch": 0, self.engine: 0}
        self.labelled_games = 0

    def add_prediction(self, prediction: dict) -> dict | None:
        """Store one worker result and return a report after both models respond."""
        game_id = prediction.get("game_id")
        model_name = prediction.get("model")
        engine = prediction.get("engine", self.engine)
        if engine not in ENGINE_MODELS:
            raise ValueError("Prediction contains an unsupported engine.")
        required_models = {"pytorch", *ENGINE_MODELS[engine]}
        if not game_id or model_name not in required_models:
            raise ValueError("Prediction must contain a game_id and a supported model name.")

        scores = self.pending_scores.setdefault(game_id, {})
        scores[model_name] = float(prediction["score"])
        if set(scores) != required_models:
            return None

        # A game is reported once every isolated worker has contributed one score.
        completed_scores = self.pending_scores.pop(game_id)
        engine_score = combine_engine_scores(engine, completed_scores)
        pytorch_prediction = int(completed_scores["pytorch"] >= 0.5)
        engine_prediction = int(engine_score >= 0.5)
        label = self._simulator_label(game_id)
        if label is not None:
            self.labelled_games += 1
            self.correct_predictions["pytorch"] += int(pytorch_prediction == label)
            self.correct_predictions.setdefault(engine, 0)
            self.correct_predictions[engine] += int(engine_prediction == label)

        return {
            "game_id": game_id,
            "engine": engine,
            "pytorch_score": completed_scores["pytorch"],
            "engine_scores": {
                model: completed_scores[model] for model in ENGINE_MODELS[engine]
            },
            "xgboost_score": engine_score,
            "pytorch_prediction": pytorch_prediction,
            "xgboost_prediction": engine_prediction,
            "pytorch_accuracy": self._accuracy("pytorch"),
            "xgboost_accuracy": self._accuracy(engine),
        }

    def _accuracy(self, model_name: str) -> float | None:
        if self.labelled_games == 0:
            return None
        return self.correct_predictions[model_name] / self.labelled_games

    @staticmethod
    def _simulator_label(game_id: str) -> int | None:
        normalized_game_id = game_id.lower()
        if "cheating" in normalized_game_id:
            return 1
        if "clean" in normalized_game_id:
            return 0
        return None

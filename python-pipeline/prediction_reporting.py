"""Join isolated model predictions into one game-level result."""


MODEL_NAMES = {"pytorch", "xgboost"}


class PredictionReporter:
    """Wait for both model workers before reporting a completed game's result."""

    def __init__(self) -> None:
        self.pending_scores: dict[str, dict[str, float]] = {}
        self.correct_predictions = {"pytorch": 0, "xgboost": 0}
        self.labelled_games = 0

    def add_prediction(self, prediction: dict) -> dict | None:
        """Store one worker result and return a report after both models respond."""
        game_id = prediction.get("game_id")
        model_name = prediction.get("model")
        if not game_id or model_name not in MODEL_NAMES:
            raise ValueError("Prediction must contain a game_id and a supported model name.")

        scores = self.pending_scores.setdefault(game_id, {})
        scores[model_name] = float(prediction["score"])
        if set(scores) != MODEL_NAMES:
            return None

        # A game is reported once both isolated workers have contributed one score.
        completed_scores = self.pending_scores.pop(game_id)
        pytorch_prediction = int(completed_scores["pytorch"] >= 0.5)
        xgboost_prediction = int(completed_scores["xgboost"] >= 0.5)
        label = self._simulator_label(game_id)
        if label is not None:
            self.labelled_games += 1
            self.correct_predictions["pytorch"] += int(pytorch_prediction == label)
            self.correct_predictions["xgboost"] += int(xgboost_prediction == label)

        return {
            "game_id": game_id,
            "pytorch_score": completed_scores["pytorch"],
            "xgboost_score": completed_scores["xgboost"],
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
        normalized_game_id = game_id.lower()
        if "cheating" in normalized_game_id:
            return 1
        if "clean" in normalized_game_id:
            return 0
        return None

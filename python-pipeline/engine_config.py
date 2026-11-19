"""Choose which engine fills the pipeline's second scoring slot.

PyTorch always runs. The second slot is XGBoost (the default), the NNUE
engine, or ``consensus``, selected with ``CHESS_ENGINE``. Consensus runs both
the XGBoost and NNUE workers and flags a game as AI only when both do.
"""

import os
from collections.abc import Mapping


DEFAULT_ENGINE = "xgboost"
ENGINE_WORKER_SCRIPTS = {
    "xgboost": ("xgboost_inference.py",),
    "nnue": ("nnue_inference.py",),
    "consensus": ("xgboost_inference.py", "nnue_inference.py"),
}
# The worker model names whose scores make up each engine's score.
ENGINE_MODELS = {
    "xgboost": ("xgboost",),
    "nnue": ("nnue",),
    "consensus": ("xgboost", "nnue"),
}


def get_selected_engine(environ: Mapping[str, str] | None = None) -> str:
    source = os.environ if environ is None else environ
    engine = source.get("CHESS_ENGINE", "").strip().lower() or DEFAULT_ENGINE
    if engine not in ENGINE_WORKER_SCRIPTS:
        choices = ", ".join(sorted(ENGINE_WORKER_SCRIPTS))
        raise ValueError(f"CHESS_ENGINE must be one of: {choices}; found {engine!r}")
    return engine


def combine_engine_scores(engine: str, scores: Mapping[str, float]) -> float:
    """Return the engine's score from its component model scores.

    Consensus uses the minimum: ``min(a, b) >= 0.5`` exactly when both
    ``a >= 0.5`` and ``b >= 0.5``, so the shared threshold rule becomes a
    logical AND, and the score stays a value in [0, 1].
    """
    return min(float(scores[model]) for model in ENGINE_MODELS[engine])

"""Validated NNUE scoring for one completed game."""

import math
from pathlib import Path
import sys

import numpy as np


MODEL_DIRECTORY = Path(__file__).resolve().parents[1] / "ml-model"
if str(MODEL_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(MODEL_DIRECTORY))

from nnue_model import NNUEAccumulator


def align_moves(moves, move_columns, expected_columns) -> np.ndarray:
    """Reorder the event's move matrix into the checkpoint's column order."""
    if not isinstance(moves, list) or not moves:
        raise ValueError("moves: the NNUE engine needs at least one move")
    if not isinstance(move_columns, list):
        raise ValueError("move_columns: expected a list of column names")
    try:
        matrix = np.asarray(moves, dtype="float64")
    except (TypeError, ValueError) as error:
        raise ValueError("moves: every value must be numeric") from error
    if matrix.ndim != 2 or matrix.shape[1] != len(move_columns):
        raise ValueError("moves: each row must have one value per move column")
    if not np.isfinite(matrix).all():
        raise ValueError("moves: every value must be finite")

    # Columns the checkpoint knows but the event lacks are zero, as in training.
    positions = {column: index for index, column in enumerate(move_columns)}
    aligned = np.zeros((len(matrix), len(expected_columns)), dtype="float32")
    for target, column in enumerate(expected_columns):
        if column in positions:
            aligned[:, target] = matrix[:, positions[column]]
    return aligned


def score_completed_game(ensemble, completed_game, feature_schema) -> float:
    """Feed one game's moves through the accumulator and return a bounded score."""
    spec = ensemble.spec
    moves = align_moves(
        completed_game.get("moves"),
        completed_game.get("move_columns"),
        spec.move_columns,
    )
    feature_values = completed_game.get("features", {})
    try:
        game_features = [float(feature_values.get(name, 0.0)) for name in feature_schema]
    except (TypeError, ValueError) as error:
        raise ValueError("feature values must be finite numbers") from error
    if not all(math.isfinite(value) for value in game_features):
        raise ValueError("feature values must be finite numbers")

    # One push per move: the accumulator only adds that move's weight columns.
    accumulator = NNUEAccumulator(ensemble)
    for move in moves:
        accumulator.push_move(move)
    score = float(accumulator.score(game_features))
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        raise ValueError("model score must be a finite value between zero and one")
    return score

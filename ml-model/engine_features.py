"""Stockfish evaluation features, defined once for the dataset tool.

The Go feature extractor computes the same four values live, so these
definitions must stay in step with ``go-backend/engine.go``:

* ``evaluation``: the position's score after the move, in centipawns from
  White's side, clamped to +/-1000. A forced mate counts as the clamp value,
  and so does a delivered checkmate (scored directly, without the engine,
  because a mated position has no search score).
* ``centipawn_loss``: how much the move worsened the mover's score, comparing
  the clamped evaluation before and after it. Never negative.
* ``win_chance``: White's expected score in percent, from the clamped
  evaluation with Lichess's logistic curve.
* ``mate``: moves to a forced mate from White's side (negative when Black
  mates), or 0 when the engine sees none.
"""

import math


EVALUATION_CLAMP = 1000
ANALYSIS_NODES = 5000
WIN_CHANCE_SLOPE = 0.00368208


def clamp_centipawns(centipawns: int | None, mate: int | None) -> int:
    """White-side centipawns, with mates mapped to the clamp value."""
    if mate is not None and mate != 0:
        return EVALUATION_CLAMP if mate > 0 else -EVALUATION_CLAMP
    if centipawns is None:
        return 0
    return max(-EVALUATION_CLAMP, min(EVALUATION_CLAMP, int(centipawns)))


def checkmate_evaluation(white_to_move: bool) -> int:
    """The side to move in a checkmated position has lost."""
    return -EVALUATION_CLAMP if white_to_move else EVALUATION_CLAMP


def win_chance(evaluation: int) -> float:
    return 50.0 + 50.0 * (2.0 / (1.0 + math.exp(-WIN_CHANCE_SLOPE * evaluation)) - 1.0)


def centipawn_loss(before: int, after: int, white_moved: bool) -> int:
    loss = before - after if white_moved else after - before
    return max(0, loss)


def move_features(before: int, after: int, after_mate: int | None, white_moved: bool) -> dict:
    """The four engine columns for one move, given clamped evaluations."""
    return {
        "centipawn_loss": centipawn_loss(before, after, white_moved),
        "evaluation": after,
        "win_chance": win_chance(after),
        "mate": int(after_mate or 0),
    }

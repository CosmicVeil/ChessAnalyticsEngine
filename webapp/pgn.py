"""Streaming PGN parsing and conversion to the existing move wire format."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import TextIO

import chess.pgn

from webapp.domain import InvalidGame, ParsedGame


class _QuietGameBuilder(chess.pgn.GameBuilder):
    def handle_error(self, error: Exception) -> None:
        self.game.errors.append(error)


def pipeline_game_id(job_id: uuid.UUID, sequence: int) -> str:
    if sequence < 1:
        raise ValueError("game sequence must be one-based")
    return f"web:{job_id}:{sequence}"


def iter_pgn_games(stream: TextIO) -> Iterator[ParsedGame | InvalidGame]:
    """Read and yield one game at a time, continuing after parse errors."""

    sequence = 0
    while True:
        game = chess.pgn.read_game(stream, Visitor=_QuietGameBuilder)
        if game is None:
            return
        sequence += 1
        if game.errors:
            yield InvalidGame(
                sequence=sequence,
                error_code="invalid_pgn",
                error_message="; ".join(str(error) for error in game.errors),
            )
            continue
        if game.end().ply() == 0:
            yield InvalidGame(
                sequence=sequence,
                error_code="empty_mainline",
                error_message="The game has no moves in its mainline.",
            )
            continue

        headers = game.headers
        yield ParsedGame(
            sequence=sequence,
            white_name=_optional_header(headers.get("White")),
            black_name=_optional_header(headers.get("Black")),
            white_elo=_rating(headers.get("WhiteElo")),
            black_elo=_rating(headers.get("BlackElo")),
            result=_optional_header(headers.get("Result")),
            played_at=_played_at(headers.get("UTCDate") or headers.get("Date")),
            opening=_optional_header(headers.get("Opening")),
            game=game,
        )


def build_move_events(
    parsed: ParsedGame, game_id: str
) -> Iterator[dict[str, object]]:
    """Yield source-ordered SAN moves for one parsed game."""

    game = parsed.game
    if not isinstance(game, chess.pgn.Game):
        raise ValueError("parsed game does not contain a PGN move tree")

    board = game.board()
    nodes = iter(game.mainline())
    current = next(nodes, None)
    move_number = 0
    while current is not None:
        following = next(nodes, None)
        move_number += 1
        san = board.san(current.move)
        clock = current.clock()
        yield {
            "game_id": game_id,
            "move": san,
            "move_number": move_number,
            "White Rating": str(parsed.white_elo or 0),
            "Black Rating": str(parsed.black_elo or 0),
            "time": 0.0 if clock is None else float(clock),
            "game_complete": following is None,
        }
        board.push(current.move)
        current = following


def _optional_header(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return None if cleaned in {"", "?"} else cleaned


def _rating(value: str | None) -> int | None:
    try:
        rating = int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return rating if rating is not None and rating >= 0 else None


def _played_at(value: str | None) -> datetime | None:
    if value is None or "?" in value:
        return None
    try:
        return datetime.strptime(value, "%Y.%m.%d").replace(tzinfo=UTC)
    except ValueError:
        return None


__all__ = ["build_move_events", "iter_pgn_games", "pipeline_game_id"]

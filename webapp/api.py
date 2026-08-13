"""JSON and durable progress endpoints for analysis jobs."""

from __future__ import annotations

import uuid

from flask import Blueprint, Response, current_app, jsonify, request, url_for
from werkzeug.exceptions import RequestEntityTooLarge

from webapp.domain import GameQuery, WebDependencies
from webapp.pgn import iter_pgn_games
from webapp.services import health_view
from webapp.spool import SourceTooLarge
from webapp.sse import stream_events


api = Blueprint("api", __name__, url_prefix="/api")


def _dependencies() -> WebDependencies:
    return current_app.extensions["web_dependencies"]


def _error(code: str, message: str, status: int, **details):
    body = {"error": {"code": code, "message": message}}
    body["error"].update(details)
    return jsonify(body), status


@api.app_errorhandler(RequestEntityTooLarge)
def request_too_large(_error_value):
    return _error("source_too_large", "The submitted PGN exceeds the configured limit.", 413)


@api.post("/analyses")
def create_analysis():
    uploaded = request.files.get("pgn_file")
    has_file = uploaded is not None and bool(uploaded.filename)
    pasted = request.form.get("pgn_text")
    has_paste = pasted is not None and pasted != ""
    lichess_url = request.form.get("lichess_url")
    has_lichess = lichess_url is not None and bool(lichess_url.strip())
    if sum((has_file, has_paste, has_lichess)) != 1:
        return _error(
            "invalid_source_selection",
            "Submit exactly one PGN file, pasted PGN, or Lichess Study URL.",
            400,
        )
    if has_lichess:
        return _error(
            "connector_unavailable",
            "Lichess Study import is reserved for a future connector.",
            501,
            url=lichess_url.strip(),
        )

    dependencies = _dependencies()
    spool = dependencies.spool_service
    source = None
    try:
        if has_file:
            source = spool.spool_file(uploaded.stream, uploaded.filename)
        else:
            source = spool.spool_text(pasted)
    except SourceTooLarge as error:
        return _error("source_too_large", str(error), 413, limit=error.limit)

    try:
        with source.spool_path.open(encoding="utf-8") as stream:
            first = next(iter_pgn_games(stream), None)
    except (OSError, UnicodeError) as error:
        source.spool_path.unlink(missing_ok=True)
        return _error("invalid_pgn", str(error), 422)
    if first is None:
        source.spool_path.unlink(missing_ok=True)
        return _error("empty_pgn", "The submitted source contains no games.", 422)

    try:
        job = dependencies.store.create_queued_job(source)
    except Exception:
        source.spool_path.unlink(missing_ok=True)
        return _error("database_unavailable", "PostgreSQL is unavailable.", 503)

    job_id = job.id
    return jsonify(
        {
            "job": job.to_view(),
            "links": {
                "page": url_for("pages.analysis", job_id=job_id),
                "status": url_for("api.get_analysis", job_id=job_id),
                "games": url_for("api.get_games", job_id=job_id),
                "events": url_for("api.get_events", job_id=job_id),
            },
        }
    ), 202


@api.get("/analyses/<uuid:job_id>")
def get_analysis(job_id: uuid.UUID):
    try:
        event_cursor = _dependencies().store.latest_event_id(job_id)
        job = _dependencies().store.get_job(job_id)
    except Exception:
        return _error("database_unavailable", "PostgreSQL is unavailable.", 503)
    if job is None:
        return _error("job_not_found", "Analysis job was not found.", 404)
    return jsonify({"job": job.to_view(), "event_cursor": event_cursor})


@api.get("/analyses/<uuid:job_id>/games")
def get_games(job_id: uuid.UUID):
    store = _dependencies().store
    try:
        if store.get_job(job_id) is None:
            return _error("job_not_found", "Analysis job was not found.", 404)
        query = _game_query()
        page = store.list_games(job_id, query)
    except ValueError as error:
        return _error("invalid_query", str(error), 400)
    except Exception:
        return _error("database_unavailable", "PostgreSQL is unavailable.", 503)
    return jsonify(
        {
            "items": [game.to_view() for game in page.items],
            "next_cursor": page.next_cursor,
            "has_more": page.has_more,
        }
    )


def _game_query() -> GameQuery:
    status = request.args.get("status", "all")
    agreement = request.args.get("agreement", "all")
    if status not in {"all", "queued", "analyzing", "completed", "failed"}:
        raise ValueError("invalid status filter")
    if agreement not in {"all", "agree", "disagree"}:
        raise ValueError("invalid agreement filter")
    score_min = _optional_float("score_min")
    score_max = _optional_float("score_max")
    if score_min is not None and not 0 <= score_min <= 1:
        raise ValueError("score_min must be between zero and one")
    if score_max is not None and not 0 <= score_max <= 1:
        raise ValueError("score_max must be between zero and one")
    if score_min is not None and score_max is not None and score_min > score_max:
        raise ValueError("score_min cannot exceed score_max")
    cursor = _optional_integer("cursor")
    limit = _optional_integer("limit", 50)
    if cursor is not None and cursor < 0:
        raise ValueError("cursor cannot be negative")
    if limit is None or limit < 1:
        raise ValueError("limit must be positive")
    return GameQuery(status, agreement, score_min, score_max, cursor, min(limit, 50))


def _optional_float(name: str) -> float | None:
    value = request.args.get(name)
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError as error:
        raise ValueError(f"{name} must be numeric") from error


def _optional_integer(name: str, default: int | None = None) -> int | None:
    value = request.args.get(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error


@api.get("/analyses/<uuid:job_id>/events")
def get_events(job_id: uuid.UUID):
    store = _dependencies().store
    try:
        if store.get_job(job_id) is None:
            return _error("job_not_found", "Analysis job was not found.", 404)
    except Exception:
        return _error("database_unavailable", "PostgreSQL is unavailable.", 503)
    raw_after = request.args.get("after", request.headers.get("Last-Event-ID", "0"))
    try:
        after_id = int(raw_after or 0)
        if after_id < 0:
            raise ValueError
    except ValueError:
        return _error("invalid_event_id", "Event ID must be a non-negative integer.", 400)
    return Response(
        stream_events(store, job_id, after_id),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@api.get("/health")
def health():
    body, status = health_view(_dependencies().health_probe)
    return jsonify(body), status


__all__ = ["api"]

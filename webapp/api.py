"""JSON and durable progress endpoints for analysis jobs."""

from __future__ import annotations

import uuid

from flask import Blueprint, Response, current_app, g, jsonify, request, url_for
from werkzeug.exceptions import RequestEntityTooLarge

from webapp.domain import GameQuery, WebDependencies
from webapp.lichess import (
    InvalidStudyUrl,
    LichessUnavailable,
    StudyForbidden,
    StudyNotFound,
    StudyRef,
    parse_study_url,
)
from webapp.pgn import iter_pgn_games
from webapp.services import health_view
from webapp.spool import SourceTooLarge
from webapp.sse import stream_events
from webapp.auth import login_required, owns_job, valid_csrf
from webapp.models import ANALYSIS_ENGINES


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
@login_required
def create_analysis():
    if not valid_csrf():
        return _error("invalid_csrf", "Your session expired. Refresh and try again.", 403)
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
    engine = request.form.get("engine", "").strip().lower()
    if engine not in ANALYSIS_ENGINES:
        return _error("invalid_engine", "Choose NNUE, XGBoost, or NNUE + XGBoost.", 400)

    study = None
    if has_lichess:
        try:
            study = parse_study_url(lichess_url)
        except InvalidStudyUrl as error:
            return _error("invalid_lichess_url", str(error), 400)

    dependencies = _dependencies()
    spool = dependencies.spool_service
    source = None
    try:
        if has_file:
            source = spool.spool_file(uploaded.stream, uploaded.filename)
        elif study is not None:
            source = _spool_lichess_study(dependencies, study)
        else:
            source = spool.spool_text(pasted)
    except SourceTooLarge as error:
        return _error("source_too_large", str(error), 413, limit=error.limit)
    except StudyNotFound as error:
        return _error("lichess_study_not_found", str(error), 404)
    except StudyForbidden as error:
        return _error("lichess_study_private", str(error), 403)
    except LichessUnavailable as error:
        return _error("lichess_unavailable", str(error), 502)

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
        job = dependencies.store.create_queued_job(source, g.current_user.id, engine)
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


def _spool_lichess_study(dependencies: WebDependencies, study: StudyRef):
    """Stream the Study's PGN export straight into the private spool."""
    response = dependencies.lichess_client.open_pgn(study)
    try:
        return dependencies.spool_service.spool_remote(response, study.source_name, "lichess")
    except OSError as error:
        raise LichessUnavailable("The Lichess download was interrupted.") from error
    finally:
        response.close()


@api.get("/analyses/<uuid:job_id>")
@login_required
def get_analysis(job_id: uuid.UUID):
    try:
        job = owns_job(job_id)
        event_cursor = _dependencies().store.latest_event_id(job_id) if job else 0
    except Exception:
        return _error("database_unavailable", "PostgreSQL is unavailable.", 503)
    if job is None:
        return _error("job_not_found", "Analysis job was not found.", 404)
    return jsonify({"job": job.to_view(), "event_cursor": event_cursor})


@api.get("/analyses/<uuid:job_id>/games")
@login_required
def get_games(job_id: uuid.UUID):
    store = _dependencies().store
    try:
        if owns_job(job_id) is None:
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
@login_required
def get_events(job_id: uuid.UUID):
    store = _dependencies().store
    try:
        if owns_job(job_id) is None:
            return _error("job_not_found", "Analysis job was not found.", 404)
    except Exception:
        return _error("database_unavailable", "PostgreSQL is unavailable.", 503)
    try:
        after_id = max(
            _event_id(request.args.get("after")),
            _event_id(request.headers.get("Last-Event-ID")),
        )
    except ValueError:
        return _error("invalid_event_id", "Event ID must be a non-negative integer.", 400)
    return Response(
        stream_events(store, job_id, after_id),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _event_id(raw_value: str | None) -> int:
    event_id = int(raw_value or 0)
    if event_id < 0:
        raise ValueError
    return event_id


@api.get("/health")
def health():
    body, status = health_view(_dependencies().health_probe)
    return jsonify(body), status


__all__ = ["api"]

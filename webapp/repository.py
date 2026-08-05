"""Transactional PostgreSQL storage for web analysis jobs."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session, selectinload, sessionmaker

from webapp.domain import (
    ClaimedJob,
    GameQuery,
    GameSnapshot,
    InvalidGame,
    JobEventSnapshot,
    JobSnapshot,
    JoinedResult,
    Page,
    ParsedGame,
    SpooledSource,
    StoreResultOutcome,
)
from webapp.models import (
    GAME_STATUS_ANALYZING,
    GAME_STATUS_COMPLETED,
    GAME_STATUS_FAILED,
    GAME_STATUS_QUEUED,
    JOB_STATUS_ANALYZING,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PARSING,
    JOB_STATUS_PARTIAL,
    JOB_STATUS_QUEUED,
    AnalysisGame,
    AnalysisJob,
    AnalysisResult,
    JobEvent,
    UnmatchedResult,
)


TERMINAL_JOB_STATUSES = frozenset(
    {JOB_STATUS_COMPLETED, JOB_STATUS_PARTIAL, JOB_STATUS_FAILED}
)
JOB_UPDATED = "job.updated"
GAME_UPDATED = "game.updated"


class SqlAlchemyAnalysisStore:
    """A short-lived-session store whose writes commit per public operation."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def create_queued_job(self, source: SpooledSource) -> JobSnapshot:
        with self._sessions.begin() as session:
            job = AnalysisJob(
                source_type=source.source_type,
                source_name=source.source_name,
                spool_path=str(source.spool_path),
                status=JOB_STATUS_QUEUED,
            )
            session.add(job)
            session.flush()
            snapshot = _job_snapshot(job)
            _add_job_event(session, job, snapshot)
        return snapshot

    def claim_next_job(
        self,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> ClaimedJob | None:
        with self._sessions.begin() as session:
            eligible = sa.or_(
                sa.and_(
                    AnalysisJob.status == JOB_STATUS_QUEUED,
                    sa.or_(
                        AnalysisJob.lease_expires_at.is_(None),
                        AnalysisJob.lease_expires_at <= now,
                    ),
                ),
                sa.and_(
                    AnalysisJob.status.in_(
                        (JOB_STATUS_PARSING, JOB_STATUS_ANALYZING)
                    ),
                    AnalysisJob.lease_expires_at.is_not(None),
                    AnalysisJob.lease_expires_at <= now,
                ),
            )
            job = session.scalar(
                sa.select(AnalysisJob)
                .where(AnalysisJob.spool_path.is_not(None), eligible)
                .order_by(AnalysisJob.created_at, AnalysisJob.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if job is None:
                return None

            lease_expires_at = now + lease_duration
            job.status = JOB_STATUS_PARSING
            job.claimed_by = worker_id
            job.lease_expires_at = lease_expires_at
            job.attempt_count += 1
            job.error_code = None
            job.error_message = None
            job.updated_at = now
            session.flush()
            _add_job_event(session, job, _job_snapshot(job))
            return ClaimedJob(
                id=job.id,
                source_type=job.source_type,
                source_name=job.source_name,
                spool_path=Path(job.spool_path),
                claimed_by=worker_id,
                lease_expires_at=lease_expires_at,
                attempt_count=job.attempt_count,
            )

    def renew_claim(
        self,
        job_id: uuid.UUID,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> bool:
        with self._sessions.begin() as session:
            job = _locked_job_or_none(session, job_id)
            if (
                job is None
                or job.claimed_by != worker_id
                or job.status in TERMINAL_JOB_STATUSES
            ):
                return False
            job.lease_expires_at = now + lease_duration
            session.flush()
            return True

    def upsert_parsed_game(
        self, job_id: uuid.UUID, game: ParsedGame
    ) -> GameSnapshot:
        with self._sessions.begin() as session:
            job = _locked_job(session, job_id)
            persisted = session.scalar(
                sa.select(AnalysisGame).where(
                    AnalysisGame.job_id == job_id,
                    AnalysisGame.sequence == game.sequence,
                )
            )
            if persisted is not None:
                return _game_snapshot(persisted)

            _reopen_for_new_sequence(job)
            persisted = AnalysisGame(
                job_id=job_id,
                sequence=game.sequence,
                pipeline_game_id=f"web:{job_id}:{game.sequence}",
                white_name=game.white_name,
                black_name=game.black_name,
                white_elo=game.white_elo,
                black_elo=game.black_elo,
                result=game.result,
                played_at=game.played_at,
                opening=game.opening,
                status=GAME_STATUS_QUEUED,
            )
            session.add(persisted)
            job.total_games += 1
            job.queued_games += 1
            job.updated_at = datetime.now(UTC)
            session.flush()
            snapshot = _game_snapshot(persisted)
            _add_game_event(session, job, persisted, snapshot)
            return snapshot

    def record_invalid_game(
        self, job_id: uuid.UUID, failure: InvalidGame
    ) -> None:
        with self._sessions.begin() as session:
            job = _locked_job(session, job_id)
            persisted = session.scalar(
                sa.select(AnalysisGame).where(
                    AnalysisGame.job_id == job_id,
                    AnalysisGame.sequence == failure.sequence,
                )
            )
            if persisted is not None:
                return

            _reopen_for_new_sequence(job)
            persisted = AnalysisGame(
                job_id=job_id,
                sequence=failure.sequence,
                pipeline_game_id=f"web:{job_id}:{failure.sequence}",
                status=GAME_STATUS_FAILED,
                error_code=failure.error_code,
                error_message=failure.error_message,
            )
            session.add(persisted)
            job.total_games += 1
            job.failed_games += 1
            job.updated_at = datetime.now(UTC)
            session.flush()
            _add_game_event(
                session, job, persisted, _game_snapshot(persisted)
            )

    def mark_game_published(self, game_id: uuid.UUID) -> None:
        with self._sessions.begin() as session:
            game = session.scalar(
                sa.select(AnalysisGame)
                .where(AnalysisGame.id == game_id)
                .with_for_update()
            )
            if game is None:
                raise KeyError(f"analysis game {game_id} does not exist")
            job = _locked_job(session, game.job_id)
            if game.status != GAME_STATUS_QUEUED:
                return

            game.status = GAME_STATUS_ANALYZING
            job.queued_games -= 1
            job.analyzing_games += 1
            if job.status not in TERMINAL_JOB_STATUSES:
                job.status = JOB_STATUS_ANALYZING
            job.updated_at = datetime.now(UTC)
            session.flush()
            _add_game_event(session, job, game, _game_snapshot(game))

    def schedule_retry(
        self,
        job_id: uuid.UUID,
        worker_id: str,
        reason: str,
        retry_at: datetime,
    ) -> None:
        with self._sessions.begin() as session:
            job = _locked_job(session, job_id)
            if (
                job.claimed_by != worker_id
                or job.status in TERMINAL_JOB_STATUSES
            ):
                return
            job.status = JOB_STATUS_QUEUED
            job.claimed_by = None
            job.lease_expires_at = retry_at
            job.error_code = "publish_retry"
            job.error_message = reason
            job.updated_at = datetime.now(UTC)
            session.flush()
            _add_job_event(session, job, _job_snapshot(job))

    def finish_ingestion(self, job_id: uuid.UUID, worker_id: str) -> None:
        with self._sessions.begin() as session:
            job = _locked_job(session, job_id)
            if job.claimed_by != worker_id:
                return

            job.claimed_by = None
            job.lease_expires_at = None
            job.spool_path = None
            if job.status in TERMINAL_JOB_STATUSES:
                return

            now = datetime.now(UTC)
            if job.total_games == 0:
                job.status = JOB_STATUS_FAILED
                job.error_code = "no_valid_games"
                job.error_message = "The source contained no valid games."
                job.completed_at = now
            elif (
                job.completed_games + job.failed_games == job.total_games
                and job.queued_games == 0
                and job.analyzing_games == 0
            ):
                job.status = _terminal_status(job)
                job.completed_at = now
            else:
                job.status = JOB_STATUS_ANALYZING
            job.updated_at = now
            session.flush()
            _add_job_event(session, job, _job_snapshot(job))

    def store_joined_result(
        self, result: JoinedResult
    ) -> StoreResultOutcome:
        with self._sessions.begin() as session:
            game = session.scalar(
                sa.select(AnalysisGame)
                .where(AnalysisGame.pipeline_game_id == result.pipeline_game_id)
                .with_for_update()
            )
            if game is None:
                return StoreResultOutcome.UNMATCHED
            if session.get(AnalysisResult, game.id) is not None:
                return StoreResultOutcome.REPLAYED

            job = _locked_job(session, game.job_id)
            analysis_result = AnalysisResult(
                game=game,
                pytorch_score=result.pytorch_score,
                xgboost_score=result.xgboost_score,
                pytorch_prediction=result.pytorch_prediction,
                xgboost_prediction=result.xgboost_prediction,
                model_payload=dict(result.model_payload),
            )
            session.add(analysis_result)
            if game.status == GAME_STATUS_QUEUED:
                job.queued_games -= 1
            elif game.status == GAME_STATUS_ANALYZING:
                job.analyzing_games -= 1
            elif game.status == GAME_STATUS_FAILED:
                job.failed_games -= 1
            game.status = GAME_STATUS_COMPLETED
            game.error_code = None
            game.error_message = None
            job.completed_games += 1

            now = datetime.now(UTC)
            if (
                job.total_games > 0
                and job.completed_games + job.failed_games == job.total_games
                and job.queued_games == 0
                and job.analyzing_games == 0
            ):
                job.status = _terminal_status(job)
                job.completed_at = now
            elif job.status not in TERMINAL_JOB_STATUSES:
                job.status = JOB_STATUS_ANALYZING
            job.updated_at = now
            session.flush()
            _add_game_event(session, job, game, _game_snapshot(game))
            return StoreResultOutcome.STORED

    def store_unmatched_result(
        self, message: object, payload: Mapping[str, Any]
    ) -> None:
        topic = str(_message_value(message, "topic"))
        partition = int(_message_value(message, "partition"))
        offset = int(_message_value(message, "offset"))
        pipeline_game_id = str(
            payload.get("game_id")
            or getattr(message, "pipeline_game_id", "")
        )
        statement = (
            postgresql_insert(UnmatchedResult)
            .values(
                id=uuid.uuid4(),
                topic=topic,
                partition=partition,
                offset=offset,
                pipeline_game_id=pipeline_game_id,
                payload=dict(payload),
            )
            .on_conflict_do_nothing(
                index_elements=["topic", "partition", "offset"]
            )
        )
        with self._sessions.begin() as session:
            session.execute(statement)

    def get_job(self, job_id: uuid.UUID) -> JobSnapshot | None:
        with self._sessions() as session:
            job = session.get(AnalysisJob, job_id)
            return None if job is None else _job_snapshot(job)

    def list_games(
        self, job_id: uuid.UUID, query: GameQuery
    ) -> Page[GameSnapshot]:
        limit = max(1, min(query.limit, 50))
        statement = (
            sa.select(AnalysisGame)
            .options(selectinload(AnalysisGame.analysis_result))
            .where(AnalysisGame.job_id == job_id)
            .order_by(AnalysisGame.sequence)
        )
        if query.cursor is not None:
            statement = statement.where(AnalysisGame.sequence > query.cursor)
        if query.status != "all":
            statement = statement.where(AnalysisGame.status == query.status)
        if (
            query.agreement != "all"
            or query.score_min is not None
            or query.score_max is not None
        ):
            statement = statement.join(AnalysisResult)
        if query.agreement == "agree":
            statement = statement.where(
                AnalysisResult.pytorch_prediction
                == AnalysisResult.xgboost_prediction
            )
        elif query.agreement == "disagree":
            statement = statement.where(
                AnalysisResult.pytorch_prediction
                != AnalysisResult.xgboost_prediction
            )
        greatest_score = sa.func.greatest(
            AnalysisResult.pytorch_score, AnalysisResult.xgboost_score
        )
        if query.score_min is not None:
            statement = statement.where(greatest_score >= query.score_min)
        if query.score_max is not None:
            statement = statement.where(greatest_score <= query.score_max)

        with self._sessions() as session:
            games = tuple(session.scalars(statement.limit(limit + 1)).all())
            has_more = len(games) > limit
            visible = games[:limit]
            items = tuple(_game_snapshot(game) for game in visible)
        return Page(
            items=items,
            next_cursor=items[-1].sequence if has_more and items else None,
            has_more=has_more,
        )

    def events_after(
        self, job_id: uuid.UUID, event_id: int, limit: int
    ) -> list[JobEventSnapshot]:
        bounded_limit = max(1, min(limit, 1000))
        statement = (
            sa.select(JobEvent)
            .where(JobEvent.job_id == job_id, JobEvent.id > event_id)
            .order_by(JobEvent.id)
            .limit(bounded_limit)
        )
        with self._sessions() as session:
            events = session.scalars(statement).all()
            return [_event_snapshot(event) for event in events]


def _locked_job(session: Session, job_id: uuid.UUID) -> AnalysisJob:
    job = _locked_job_or_none(session, job_id)
    if job is None:
        raise KeyError(f"analysis job {job_id} does not exist")
    return job


def _locked_job_or_none(
    session: Session, job_id: uuid.UUID
) -> AnalysisJob | None:
    return session.scalar(
        sa.select(AnalysisJob)
        .where(AnalysisJob.id == job_id)
        .with_for_update()
    )


def _terminal_status(job: AnalysisJob) -> str:
    if job.completed_games and job.failed_games:
        return JOB_STATUS_PARTIAL
    if job.completed_games:
        return JOB_STATUS_COMPLETED
    return JOB_STATUS_FAILED


def _reopen_for_new_sequence(job: AnalysisJob) -> None:
    if job.status not in TERMINAL_JOB_STATUSES:
        return
    job.status = JOB_STATUS_PARSING
    job.completed_at = None
    job.error_code = None
    job.error_message = None


def _job_snapshot(job: AnalysisJob) -> JobSnapshot:
    return JobSnapshot(
        id=job.id,
        source_type=job.source_type,
        source_name=job.source_name,
        status=job.status,
        total_games=job.total_games,
        queued_games=job.queued_games,
        analyzing_games=job.analyzing_games,
        completed_games=job.completed_games,
        failed_games=job.failed_games,
        created_at=job.created_at,
        updated_at=job.updated_at,
        completed_at=job.completed_at,
    )


def _game_snapshot(game: AnalysisGame) -> GameSnapshot:
    result = game.analysis_result
    return GameSnapshot(
        id=game.id,
        job_id=game.job_id,
        sequence=game.sequence,
        pipeline_game_id=game.pipeline_game_id,
        status=game.status,
        white_name=game.white_name,
        black_name=game.black_name,
        white_elo=game.white_elo,
        black_elo=game.black_elo,
        result=game.result,
        played_at=game.played_at,
        opening=game.opening,
        error_code=game.error_code,
        error_message=game.error_message,
        pytorch_score=None if result is None else result.pytorch_score,
        xgboost_score=None if result is None else result.xgboost_score,
        pytorch_prediction=(
            None if result is None else result.pytorch_prediction
        ),
        xgboost_prediction=(
            None if result is None else result.xgboost_prediction
        ),
    )


def _event_snapshot(event: JobEvent) -> JobEventSnapshot:
    return JobEventSnapshot(
        id=event.id,
        job_id=event.job_id,
        game_id=event.game_id,
        event_type=event.event_type,
        payload=dict(event.payload),
        created_at=event.created_at,
    )


def _add_job_event(
    session: Session, job: AnalysisJob, snapshot: JobSnapshot
) -> None:
    session.add(
        JobEvent(
            job_id=job.id,
            event_type=JOB_UPDATED,
            payload={"job": snapshot.to_view()},
        )
    )


def _add_game_event(
    session: Session,
    job: AnalysisJob,
    game: AnalysisGame,
    snapshot: GameSnapshot,
) -> None:
    session.add(
        JobEvent(
            job_id=job.id,
            game_id=game.id,
            event_type=GAME_UPDATED,
            payload={"job": _job_snapshot(job).to_view(), "game": snapshot.to_view()},
        )
    )


def _message_value(message: object, name: str) -> object:
    value = getattr(message, name)
    return value() if callable(value) else value


__all__ = ["SqlAlchemyAnalysisStore"]

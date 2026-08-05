"""Deterministic in-memory collaborators for web adapter unit tests."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

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


TERMINAL_STATUSES = frozenset({"completed", "partial", "failed"})


class FakeAnalysisStore:
    """Small stateful implementation of the store protocol for worker tests."""

    def __init__(self) -> None:
        self.jobs: dict[uuid.UUID, JobSnapshot] = {}
        self.games: dict[uuid.UUID, GameSnapshot] = {}
        self.sources: dict[uuid.UUID, SpooledSource] = {}
        self.claims: dict[uuid.UUID, ClaimedJob] = {}
        self.available_at: dict[uuid.UUID, datetime] = {}
        self.events: list[JobEventSnapshot] = []
        self.unmatched_results: list[tuple[object, dict[str, Any]]] = []
        self._next_event_id = 1

    def create_queued_job(self, source: SpooledSource) -> JobSnapshot:
        now = datetime.now(UTC)
        job = JobSnapshot(
            id=uuid.uuid4(),
            source_type=source.source_type,
            source_name=source.source_name,
            status="queued",
            total_games=0,
            queued_games=0,
            analyzing_games=0,
            completed_games=0,
            failed_games=0,
            created_at=now,
            updated_at=now,
            completed_at=None,
        )
        self.jobs[job.id] = job
        self.sources[job.id] = source
        self._emit_job(job)
        return job

    def claim_next_job(
        self,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> ClaimedJob | None:
        for job in sorted(self.jobs.values(), key=lambda item: (item.created_at, item.id)):
            previous = self.claims.get(job.id)
            queued_ready = job.status == "queued" and self.available_at.get(
                job.id, now
            ) <= now
            expired = (
                job.status in {"parsing", "live"}
                and previous is not None
                and previous.lease_expires_at <= now
            )
            if not (queued_ready or expired) or job.id not in self.sources:
                continue
            attempt_count = 1 if previous is None else previous.attempt_count + 1
            claim = ClaimedJob(
                id=job.id,
                source_type=job.source_type,
                source_name=job.source_name,
                spool_path=self.sources[job.id].spool_path,
                claimed_by=worker_id,
                lease_expires_at=now + lease_duration,
                attempt_count=attempt_count,
            )
            self.claims[job.id] = claim
            updated = replace(job, status="parsing", updated_at=now)
            self.jobs[job.id] = updated
            self._emit_job(updated)
            return claim
        return None

    def renew_claim(
        self,
        job_id: uuid.UUID,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> bool:
        claim = self.claims.get(job_id)
        if (
            claim is None
            or claim.claimed_by != worker_id
            or self.jobs[job_id].status in TERMINAL_STATUSES
        ):
            return False
        self.claims[job_id] = replace(
            claim, lease_expires_at=now + lease_duration
        )
        return True

    def upsert_parsed_game(
        self, job_id: uuid.UUID, game: ParsedGame
    ) -> GameSnapshot:
        existing = self._game_for_sequence(job_id, game.sequence)
        if existing is not None:
            return existing
        job = self._reopen(self.jobs[job_id])
        snapshot = GameSnapshot(
            id=uuid.uuid4(),
            job_id=job_id,
            sequence=game.sequence,
            pipeline_game_id=f"web:{job_id}:{game.sequence}",
            status="queued",
            white_name=game.white_name,
            black_name=game.black_name,
            white_elo=game.white_elo,
            black_elo=game.black_elo,
            result=game.result,
            played_at=game.played_at,
            opening=game.opening,
        )
        job = replace(
            job,
            total_games=job.total_games + 1,
            queued_games=job.queued_games + 1,
            updated_at=datetime.now(UTC),
        )
        self.jobs[job_id] = job
        self.games[snapshot.id] = snapshot
        self._emit_game(job, snapshot)
        return snapshot

    def record_invalid_game(
        self, job_id: uuid.UUID, failure: InvalidGame
    ) -> None:
        if self._game_for_sequence(job_id, failure.sequence) is not None:
            return
        job = self._reopen(self.jobs[job_id])
        game = GameSnapshot(
            id=uuid.uuid4(),
            job_id=job_id,
            sequence=failure.sequence,
            pipeline_game_id=f"web:{job_id}:{failure.sequence}",
            status="failed",
            error_code=failure.error_code,
            error_message=failure.error_message,
        )
        job = replace(
            job,
            total_games=job.total_games + 1,
            failed_games=job.failed_games + 1,
            updated_at=datetime.now(UTC),
        )
        self.jobs[job_id] = job
        self.games[game.id] = game
        self._emit_game(job, game)

    def mark_game_published(self, game_id: uuid.UUID) -> None:
        game = self.games[game_id]
        if game.status != "queued":
            return
        job = self.jobs[game.job_id]
        game = replace(game, status="analyzing")
        job = replace(
            job,
            status="live",
            queued_games=job.queued_games - 1,
            analyzing_games=job.analyzing_games + 1,
            updated_at=datetime.now(UTC),
        )
        self.games[game_id] = game
        self.jobs[job.id] = job
        self._emit_game(job, game)

    def schedule_retry(
        self,
        job_id: uuid.UUID,
        worker_id: str,
        reason: str,
        retry_at: datetime,
    ) -> None:
        claim = self.claims.get(job_id)
        job = self.jobs[job_id]
        if claim is None or claim.claimed_by != worker_id or job.status in TERMINAL_STATUSES:
            return
        self.claims.pop(job_id, None)
        self.available_at[job_id] = retry_at
        job = replace(job, status="queued", updated_at=datetime.now(UTC))
        self.jobs[job_id] = job
        self._emit_job(job)

    def finish_ingestion(self, job_id: uuid.UUID, worker_id: str) -> None:
        claim = self.claims.get(job_id)
        if claim is None or claim.claimed_by != worker_id:
            return
        self.claims.pop(job_id, None)
        self.sources.pop(job_id, None)
        job = self.jobs[job_id]
        if job.status in TERMINAL_STATUSES:
            return
        now = datetime.now(UTC)
        if job.total_games == 0 or job.completed_games + job.failed_games == job.total_games:
            status = self._terminal_status(job)
            job = replace(job, status=status, updated_at=now, completed_at=now)
        else:
            job = replace(job, status="live", updated_at=now)
        self.jobs[job_id] = job
        self._emit_job(job)

    def store_joined_result(
        self, result: JoinedResult
    ) -> StoreResultOutcome:
        game = next(
            (
                candidate
                for candidate in self.games.values()
                if candidate.pipeline_game_id == result.pipeline_game_id
            ),
            None,
        )
        if game is None:
            return StoreResultOutcome.UNMATCHED
        if game.pytorch_score is not None:
            return StoreResultOutcome.REPLAYED
        job = self.jobs[game.job_id]
        queued_delta = 1 if game.status == "queued" else 0
        analyzing_delta = 1 if game.status == "analyzing" else 0
        failed_delta = 1 if game.status == "failed" else 0
        game = replace(
            game,
            status="completed",
            error_code=None,
            error_message=None,
            pytorch_score=result.pytorch_score,
            xgboost_score=result.xgboost_score,
            pytorch_prediction=result.pytorch_prediction,
            xgboost_prediction=result.xgboost_prediction,
        )
        now = datetime.now(UTC)
        job = replace(
            job,
            queued_games=job.queued_games - queued_delta,
            analyzing_games=job.analyzing_games - analyzing_delta,
            failed_games=job.failed_games - failed_delta,
            completed_games=job.completed_games + 1,
            updated_at=now,
        )
        if (
            job.id not in self.sources
            and job.total_games > 0
            and job.completed_games + job.failed_games == job.total_games
            and job.queued_games == 0
            and job.analyzing_games == 0
        ):
            job = replace(
                job,
                status=self._terminal_status(job),
                completed_at=now,
            )
        elif job.status not in TERMINAL_STATUSES:
            job = replace(job, status="live")
        self.games[game.id] = game
        self.jobs[job.id] = job
        self._emit_game(job, game)
        return StoreResultOutcome.STORED

    def store_unmatched_result(
        self, message: object, payload: Mapping[str, Any]
    ) -> None:
        coordinates = (
            self._message_value(message, "topic"),
            self._message_value(message, "partition"),
            self._message_value(message, "offset"),
        )
        if any(item[0] == coordinates for item in self.unmatched_results):
            return
        self.unmatched_results.append((coordinates, dict(payload)))

    def get_job(self, job_id: uuid.UUID) -> JobSnapshot | None:
        return self.jobs.get(job_id)

    def list_games(
        self, job_id: uuid.UUID, query: GameQuery
    ) -> Page[GameSnapshot]:
        games = sorted(
            (game for game in self.games.values() if game.job_id == job_id),
            key=lambda game: game.sequence,
        )
        if query.cursor is not None:
            games = [game for game in games if game.sequence > query.cursor]
        if query.status != "all":
            games = [game for game in games if game.status == query.status]
        if query.agreement != "all":
            agrees = query.agreement == "agree"
            games = [
                game
                for game in games
                if game.pytorch_prediction is not None
                and (game.pytorch_prediction == game.xgboost_prediction) is agrees
            ]
        if query.score_min is not None:
            games = [game for game in games if self._score(game) >= query.score_min]
        if query.score_max is not None:
            games = [game for game in games if self._score(game) <= query.score_max]
        limit = max(1, min(query.limit, 50))
        has_more = len(games) > limit
        items = tuple(games[:limit])
        return Page(
            items=items,
            next_cursor=items[-1].sequence if has_more and items else None,
            has_more=has_more,
        )

    def events_after(
        self, job_id: uuid.UUID, event_id: int, limit: int
    ) -> list[JobEventSnapshot]:
        return [
            event
            for event in self.events
            if event.job_id == job_id and event.id > event_id
        ][: max(1, min(limit, 1000))]

    def _emit_job(self, job: JobSnapshot) -> None:
        self._emit(job, None, "job.updated", {"job": job.to_view()})

    def _emit_game(self, job: JobSnapshot, game: GameSnapshot) -> None:
        self._emit(
            job,
            game,
            "game.updated",
            {"job": job.to_view(), "game": game.to_view()},
        )

    def _emit(
        self,
        job: JobSnapshot,
        game: GameSnapshot | None,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        self.events.append(
            JobEventSnapshot(
                id=self._next_event_id,
                job_id=job.id,
                game_id=None if game is None else game.id,
                event_type=event_type,
                payload=payload,
                created_at=datetime.now(UTC),
            )
        )
        self._next_event_id += 1

    def _game_for_sequence(
        self, job_id: uuid.UUID, sequence: int
    ) -> GameSnapshot | None:
        return next(
            (
                game
                for game in self.games.values()
                if game.job_id == job_id and game.sequence == sequence
            ),
            None,
        )

    @staticmethod
    def _reopen(job: JobSnapshot) -> JobSnapshot:
        if job.status not in TERMINAL_STATUSES:
            return job
        return replace(job, status="parsing", completed_at=None)

    @staticmethod
    def _terminal_status(job: JobSnapshot) -> str:
        if job.completed_games and job.failed_games:
            return "partial"
        if job.completed_games:
            return "completed"
        return "failed"

    @staticmethod
    def _message_value(message: object, name: str) -> object:
        value = getattr(message, name)
        return value() if callable(value) else value

    @staticmethod
    def _score(game: GameSnapshot) -> float:
        scores = [
            score
            for score in (game.pytorch_score, game.xgboost_score)
            if score is not None
        ]
        return max(scores) if scores else -1.0


__all__ = ["FakeAnalysisStore"]

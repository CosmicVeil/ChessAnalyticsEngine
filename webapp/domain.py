"""Dependency boundaries and immutable values for the Flask adapter."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar, runtime_checkable


T = TypeVar("T")


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


@dataclass(frozen=True, slots=True)
class SpooledSource:
    source_type: str
    source_name: str
    spool_path: Path


@dataclass(frozen=True, slots=True)
class ParsedGame:
    sequence: int
    white_name: str | None = None
    black_name: str | None = None
    white_elo: int | None = None
    black_elo: int | None = None
    result: str | None = None
    played_at: datetime | None = None
    opening: str | None = None
    game: Any = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class InvalidGame:
    sequence: int
    error_code: str
    error_message: str


@dataclass(frozen=True, slots=True)
class JobSnapshot:
    id: uuid.UUID
    source_type: str
    source_name: str
    status: str
    total_games: int
    queued_games: int
    analyzing_games: int
    completed_games: int
    failed_games: int
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    def to_view(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "source_type": self.source_type,
            "source_name": self.source_name,
            "status": self.status,
            "counts": {
                "total": self.total_games,
                "queued": self.queued_games,
                "analyzing": self.analyzing_games,
                "completed": self.completed_games,
                "failed": self.failed_games,
            },
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "completed_at": _iso(self.completed_at),
        }


@dataclass(frozen=True, slots=True)
class GameSnapshot:
    id: uuid.UUID
    job_id: uuid.UUID
    sequence: int
    pipeline_game_id: str
    status: str
    white_name: str | None = None
    black_name: str | None = None
    white_elo: int | None = None
    black_elo: int | None = None
    result: str | None = None
    played_at: datetime | None = None
    opening: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    pytorch_score: float | None = None
    xgboost_score: float | None = None
    pytorch_prediction: bool | None = None
    xgboost_prediction: bool | None = None

    def to_view(self) -> dict[str, Any]:
        error = None
        if self.error_code is not None or self.error_message is not None:
            error = {"code": self.error_code, "message": self.error_message}
        scores = None
        if (
            self.pytorch_score is not None
            and self.xgboost_score is not None
            and self.pytorch_prediction is not None
            and self.xgboost_prediction is not None
        ):
            scores = {
                "pytorch": {
                    "score": self.pytorch_score,
                    "prediction": self.pytorch_prediction,
                },
                "xgboost": {
                    "score": self.xgboost_score,
                    "prediction": self.xgboost_prediction,
                },
                "agreement": self.pytorch_prediction
                == self.xgboost_prediction,
            }
        return {
            "id": str(self.id),
            "sequence": self.sequence,
            "status": self.status,
            "white": {"name": self.white_name, "elo": self.white_elo},
            "black": {"name": self.black_name, "elo": self.black_elo},
            "result": self.result,
            "played_at": _iso(self.played_at),
            "opening": self.opening,
            "error": error,
            "scores": scores,
        }


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    id: uuid.UUID
    source_type: str
    source_name: str
    spool_path: Path
    claimed_by: str
    lease_expires_at: datetime
    attempt_count: int


@dataclass(frozen=True, slots=True)
class JoinedResult:
    pipeline_game_id: str
    pytorch_score: float
    xgboost_score: float
    pytorch_prediction: bool
    xgboost_prediction: bool
    model_payload: Mapping[str, Any] = field(default_factory=dict)


class StoreResultOutcome(str, Enum):
    STORED = "stored"
    REPLAYED = "replayed"
    UNMATCHED = "unmatched"


@dataclass(frozen=True, slots=True)
class JobEventSnapshot:
    id: int
    job_id: uuid.UUID
    game_id: uuid.UUID | None
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class GameQuery:
    status: str = "all"
    agreement: str = "all"
    score_min: float | None = None
    score_max: float | None = None
    cursor: int | None = None
    limit: int = 50


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    items: tuple[T, ...]
    next_cursor: int | None
    has_more: bool


@runtime_checkable
class AnalysisStore(Protocol):
    def create_queued_job(self, source: SpooledSource) -> JobSnapshot: ...

    def claim_next_job(
        self,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> ClaimedJob | None: ...

    def renew_claim(
        self,
        job_id: uuid.UUID,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> bool: ...

    def upsert_parsed_game(
        self, job_id: uuid.UUID, game: ParsedGame
    ) -> GameSnapshot: ...

    def record_invalid_game(
        self, job_id: uuid.UUID, failure: InvalidGame
    ) -> None: ...

    def mark_game_published(self, game_id: uuid.UUID) -> None: ...

    def schedule_retry(
        self,
        job_id: uuid.UUID,
        worker_id: str,
        reason: str,
        retry_at: datetime,
    ) -> None: ...

    def finish_ingestion(self, job_id: uuid.UUID, worker_id: str) -> None: ...

    def store_joined_result(
        self, result: JoinedResult
    ) -> StoreResultOutcome: ...

    def store_unmatched_result(
        self, message: object, payload: Mapping[str, Any]
    ) -> None: ...

    def get_job(self, job_id: uuid.UUID) -> JobSnapshot | None: ...

    def list_games(
        self, job_id: uuid.UUID, query: GameQuery
    ) -> Page[GameSnapshot]: ...

    def events_after(
        self, job_id: uuid.UUID, event_id: int, limit: int
    ) -> list[JobEventSnapshot]: ...


@dataclass(frozen=True, slots=True)
class WebDependencies:
    """Runtime collaborators, replaceable independently in tests."""

    store: object | None = None
    spool_service: object | None = None
    health_probe: object | None = None


__all__ = [
    "AnalysisStore",
    "ClaimedJob",
    "GameQuery",
    "GameSnapshot",
    "InvalidGame",
    "JobEventSnapshot",
    "JobSnapshot",
    "JoinedResult",
    "Page",
    "ParsedGame",
    "SpooledSource",
    "StoreResultOutcome",
    "WebDependencies",
]

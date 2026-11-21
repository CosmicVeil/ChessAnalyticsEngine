"""Rate limits whose counters live in PostgreSQL, shared by every web worker.

gunicorn runs several processes, so an in-memory counter would give each
worker its own budget. Two shared sources of truth are used instead:

* IP-keyed auth limits use fixed-window counters in ``rate_limit_counters``,
  bumped with one atomic ``INSERT ... ON CONFLICT DO UPDATE``.
* Per-account analysis limits count rows the app already writes: the
  ``analysis_jobs`` table *is* the log of analyses a user started.
"""

from __future__ import annotations

import math
import random
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session, sessionmaker

from webapp.domain import AnalysisQuotaUsage
from webapp.models import RateLimitCounter


# Longest window in use is one hour; anything a day old is safe to drop.
_COUNTER_RETENTION = timedelta(days=1)
_SWEEP_PROBABILITY = 0.01
# A user at the concurrent-job cap must wait for a job to finish; there is
# no exact time to report, so suggest a short retry.
ACTIVE_JOB_RETRY_SECONDS = 60


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    retry_after: int = 0
    message: str = ""


ALLOWED = RateLimitDecision(True)


def _window_start(now: datetime, window_seconds: int) -> datetime:
    epoch = int(now.timestamp())
    return datetime.fromtimestamp(epoch - epoch % window_seconds, UTC)


def _retry_after(now: datetime, reset_at: datetime) -> int:
    return max(1, math.ceil((reset_at - now).total_seconds()))


def _decision(
    hits: int, limit: int, now: datetime, window_start: datetime, window_seconds: int
) -> RateLimitDecision:
    if hits <= limit:
        return ALLOWED
    reset_at = window_start + timedelta(seconds=window_seconds)
    return RateLimitDecision(False, _retry_after(now, reset_at))


class PostgresRateLimiter:
    """Fixed-window counters shared across processes through PostgreSQL."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def hit(
        self,
        key: str,
        limit: int,
        window_seconds: int,
        now: datetime | None = None,
    ) -> RateLimitDecision:
        now = now or datetime.now(UTC)
        window_start = _window_start(now, window_seconds)
        statement = (
            postgresql_insert(RateLimitCounter)
            .values(key=key, window_start=window_start, hits=1)
            .on_conflict_do_update(
                index_elements=[RateLimitCounter.key, RateLimitCounter.window_start],
                set_={"hits": RateLimitCounter.hits + 1},
            )
            .returning(RateLimitCounter.hits)
        )
        with self._sessions.begin() as session:
            hits = session.execute(statement).scalar_one()
            if random.random() < _SWEEP_PROBABILITY:
                session.execute(
                    sa.delete(RateLimitCounter).where(
                        RateLimitCounter.window_start < now - _COUNTER_RETENTION
                    )
                )
        return _decision(hits, limit, now, window_start, window_seconds)


class MemoryRateLimiter:
    """Per-process counters, used only with injected test collaborators."""

    def __init__(self) -> None:
        self._hits: dict[tuple[str, datetime], int] = {}
        self._lock = threading.Lock()

    def hit(
        self,
        key: str,
        limit: int,
        window_seconds: int,
        now: datetime | None = None,
    ) -> RateLimitDecision:
        now = now or datetime.now(UTC)
        window_start = _window_start(now, window_seconds)
        with self._lock:
            hits = self._hits.get((key, window_start), 0) + 1
            self._hits[(key, window_start)] = hits
        return _decision(hits, limit, now, window_start, window_seconds)


def _wait_text(seconds: int) -> str:
    minutes = math.ceil(seconds / 60)
    if minutes <= 1:
        return "a minute"
    return f"{minutes} minutes"


def rate_limited_message(action: str, retry_after: int) -> str:
    return f"Too many {action}. Try again in {_wait_text(retry_after)}."


def check_analysis_quota(
    usage: AnalysisQuotaUsage,
    *,
    per_hour: int,
    max_active: int,
    now: datetime | None = None,
) -> RateLimitDecision:
    """Decide whether an account may start one more analysis."""

    now = now or datetime.now(UTC)
    if usage.active_jobs >= max_active:
        return RateLimitDecision(
            False,
            ACTIVE_JOB_RETRY_SECONDS,
            f"You already have {usage.active_jobs} analyses running "
            f"(limit {max_active}). Wait for one to finish, then try again.",
        )
    if usage.recent_jobs >= per_hour:
        oldest = usage.oldest_recent_created_at or now
        retry_after = _retry_after(now, oldest + timedelta(hours=1))
        return RateLimitDecision(
            False,
            retry_after,
            f"You can start {per_hour} analyses per hour. "
            f"Try again in {_wait_text(retry_after)}.",
        )
    return ALLOWED


__all__ = [
    "ALLOWED",
    "MemoryRateLimiter",
    "PostgresRateLimiter",
    "RateLimitDecision",
    "check_analysis_quota",
    "rate_limited_message",
]

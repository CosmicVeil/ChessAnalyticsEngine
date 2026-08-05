"""Durable Server-Sent Event formatting and polling."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Iterator

from webapp.domain import AnalysisStore, JobEventSnapshot


TERMINAL_STATUSES = frozenset({"completed", "partial", "failed"})


def format_sse(event: JobEventSnapshot) -> bytes:
    payload = json.dumps(event.payload, separators=(",", ":"))
    return (
        f"id: {event.id}\n"
        f"event: {event.event_type}\n"
        f"data: {payload}\n\n"
    ).encode("utf-8")


def stream_events(
    store: AnalysisStore,
    job_id: uuid.UUID,
    after_id: int = 0,
    wait: Callable[[], None] | None = None,
    *,
    batch_limit: int = 100,
) -> Iterator[bytes]:
    pause = wait or (lambda: time.sleep(1.0))
    cursor = after_id
    while True:
        events = store.events_after(job_id, cursor, batch_limit)
        if events:
            for event in events:
                cursor = event.id
                yield format_sse(event)
            job = store.get_job(job_id)
            if (
                len(events) < batch_limit
                and job is not None
                and job.status in TERMINAL_STATUSES
            ):
                return
            continue

        job = store.get_job(job_id)
        if job is None or job.status in TERMINAL_STATUSES:
            return
        yield b": keepalive\n\n"
        pause()


__all__ = ["format_sse", "stream_events"]

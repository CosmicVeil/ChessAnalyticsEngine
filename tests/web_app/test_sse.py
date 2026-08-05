"""Durable SSE formatting, reconnection, keepalive, and endpoint tests."""

import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.web_app.fakes import FakeAnalysisStore
from webapp import create_app
from webapp.config import Settings
from webapp.domain import JobEventSnapshot, SpooledSource, WebDependencies
from webapp.sse import format_sse, stream_events


class SseTests(unittest.TestCase):
    def test_format_is_compact_and_resumable(self):
        event = JobEventSnapshot(
            id=42,
            job_id=uuid.uuid4(),
            game_id=None,
            event_type="job.updated",
            payload={"job": {"status": "queued"}},
            created_at=datetime(2026, 8, 4, tzinfo=UTC),
        )
        self.assertEqual(
            format_sse(event),
            b'id: 42\nevent: job.updated\ndata: {"job":{"status":"queued"}}\n\n',
        )

    def test_stream_replays_after_id_and_closes_at_terminal_job(self):
        store = FakeAnalysisStore()
        job = store.create_queued_job(
            SpooledSource("paste", "pgn", Path("/tmp/x"))
        )
        store.claim_next_job(
            "w", datetime(2026, 8, 4, tzinfo=UTC), timedelta(minutes=1)
        )
        store.finish_ingestion(job.id, "w")
        events = list(stream_events(store, job.id, after_id=1, wait=lambda: None))
        self.assertTrue(events)
        self.assertTrue(all(not event.startswith(b"id: 1\n") for event in events))

    def test_endpoint_prefers_query_cursor_and_sets_stream_headers(self):
        store = FakeAnalysisStore()
        job = store.create_queued_job(
            SpooledSource("paste", "pgn", Path("/tmp/x"))
        )
        store.claim_next_job(
            "w", datetime(2026, 8, 4, tzinfo=UTC), timedelta(minutes=1)
        )
        store.finish_ingestion(job.id, "w")
        app = create_app(
            Settings(database_url="postgresql+psycopg://unused"),
            WebDependencies(store=store, spool_service=object(), health_probe=object()),
        )
        client = app.test_client()

        response = client.get(
            f"/api/analyses/{job.id}/events?after=1",
            headers={"Last-Event-ID": "0"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.mimetype, "text/event-stream")
        self.assertEqual(response.headers["X-Accel-Buffering"], "no")
        self.assertNotIn(b"id: 1\n", response.data)
        self.assertEqual(client.get(f"/api/analyses/{job.id}/events?after=-1").status_code, 400)
        self.assertEqual(client.get("/api/analyses/not-a-uuid/events").status_code, 404)


if __name__ == "__main__":
    unittest.main()

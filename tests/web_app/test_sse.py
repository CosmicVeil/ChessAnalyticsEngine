"""Durable SSE formatting, reconnection, keepalive, and endpoint tests."""

import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.web_app.fakes import FakeAnalysisStore
from webapp import create_app
from webapp.config import Settings
from webapp.domain import (
    InvalidGame,
    JobEventSnapshot,
    ParsedGame,
    SpooledSource,
    WebDependencies,
)
from webapp.sse import format_sse, stream_events


class SseTests(unittest.TestCase):
    @staticmethod
    def _authenticated_client(store):
        app = create_app(
            Settings(database_url="postgresql+psycopg://unused", secret_key="test"),
            WebDependencies(store=store, spool_service=object(), health_probe=object()),
        )
        client = app.test_client()
        client.get("/create-account")
        with client.session_transaction() as session:
            csrf = session["csrf_token"]
        client.post("/create-account", data={
            "csrf_token": csrf,
            "display_name": "SSE Tester",
            "email": f"sse-{uuid.uuid4()}@example.com",
            "password": "correct horse battery staple",
            "preferred_engine": "nnue",
        })
        with client.session_transaction() as session:
            return client, uuid.UUID(session["user_id"])

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

    def test_stream_stays_open_while_batch_is_live(self):
        store = FakeAnalysisStore()
        job = store.create_queued_job(
            SpooledSource("paste", "pgn", Path("/tmp/x"))
        )
        store.claim_next_job(
            "w", datetime(2026, 8, 4, tzinfo=UTC), timedelta(minutes=1)
        )
        game = store.upsert_parsed_game(job.id, ParsedGame(sequence=1))
        store.mark_game_published(game.id)
        self.assertEqual(store.get_job(job.id).status, "live")

        stream = stream_events(
            store,
            job.id,
            after_id=store.events[-1].id,
            wait=lambda: None,
        )
        self.assertEqual(next(stream), b": keepalive\n\n")
        stream.close()

    def _terminal_endpoint_client(self):
        store = FakeAnalysisStore()
        client, owner_id = self._authenticated_client(store)
        job = store.create_queued_job(
            SpooledSource("paste", "pgn", Path("/tmp/x")), owner_id
        )
        store.claim_next_job(
            "w", datetime(2026, 8, 4, tzinfo=UTC), timedelta(minutes=1)
        )
        store.finish_ingestion(job.id, "w")
        return store, job, client

    def test_endpoint_uses_greater_query_cursor_and_sets_stream_headers(self):
        _store, job, client = self._terminal_endpoint_client()

        response = client.get(
            f"/api/analyses/{job.id}/events?after=2",
            headers={"Last-Event-ID": "1"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.mimetype, "text/event-stream")
        self.assertEqual(response.headers["X-Accel-Buffering"], "no")
        self.assertNotIn(b"id: 2\n", response.data)
        self.assertIn(b"id: 3\n", response.data)

    def test_endpoint_uses_greater_reconnect_header_cursor(self):
        _store, job, client = self._terminal_endpoint_client()

        response = client.get(
            f"/api/analyses/{job.id}/events?after=1",
            headers={"Last-Event-ID": "2"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"id: 2\n", response.data)
        self.assertIn(b"id: 3\n", response.data)

    def test_endpoint_rejects_invalid_or_negative_cursor_from_either_source(self):
        _store, job, client = self._terminal_endpoint_client()

        invalid_urls_and_headers = (
            (f"/api/analyses/{job.id}/events?after=-1", {}),
            (f"/api/analyses/{job.id}/events?after=invalid", {}),
            (f"/api/analyses/{job.id}/events?after=1", {"Last-Event-ID": "-1"}),
            (f"/api/analyses/{job.id}/events?after=1", {"Last-Event-ID": "invalid"}),
        )
        for url, headers in invalid_urls_and_headers:
            with self.subTest(url=url, headers=headers):
                self.assertEqual(client.get(url, headers=headers).status_code, 400)

        self.assertEqual(client.get("/api/analyses/not-a-uuid/events").status_code, 404)

    def test_status_cursor_replays_only_events_committed_after_the_snapshot(self):
        store = FakeAnalysisStore()
        client, owner_id = self._authenticated_client(store)
        job = store.create_queued_job(
            SpooledSource("paste", "pgn", Path("/tmp/x")), owner_id
        )
        store.claim_next_job(
            "w", datetime(2026, 8, 4, tzinfo=UTC), timedelta(minutes=1)
        )
        status = client.get(f"/api/analyses/{job.id}").get_json()
        cursor = status["event_cursor"]
        historical_ids = [event.id for event in store.events]

        store.record_invalid_game(
            job.id, InvalidGame(1, "invalid_pgn", "illegal move")
        )
        store.finish_ingestion(job.id, "w")
        new_ids = [event.id for event in store.events if event.id > cursor]

        response = client.get(f"/api/analyses/{job.id}/events?after={cursor}")

        self.assertTrue(new_ids)
        for event_id in historical_ids:
            self.assertNotIn(f"id: {event_id}\n".encode(), response.data)
        for event_id in new_ids:
            self.assertIn(f"id: {event_id}\n".encode(), response.data)


if __name__ == "__main__":
    unittest.main()

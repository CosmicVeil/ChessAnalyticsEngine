"""PGN ingestion worker behavior with in-memory collaborators."""

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.web_app.fakes import FakeAnalysisStore
from webapp.domain import SpooledSource
from webapp.ingestion_worker import IngestionWorker


NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


class RecordingPublisher:
    def __init__(self, fail=False):
        self.games = []
        self.fail = fail

    def publish_game(self, game_id, events):
        if self.fail:
            raise RuntimeError("broker unavailable")
        self.games.append((game_id, list(events)))


class IngestionWorkerTests(unittest.TestCase):
    def make_job(self, directory, pgn):
        path = Path(directory) / "source.pgn"
        path.write_text(pgn, encoding="utf-8")
        store = FakeAnalysisStore()
        job = store.create_queued_job(SpooledSource("file", "source.pgn", path))
        return store, job, path

    def test_streams_games_in_source_order_then_removes_spool(self):
        pgn = Path("tests/fixtures/webapp/small_batch.pgn").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            store, job, path = self.make_job(directory, pgn)
            publisher = RecordingPublisher()
            worker = IngestionWorker(store, publisher, worker_id="worker-a")

            self.assertTrue(worker.process_one(NOW))

            self.assertEqual([item[0] for item in publisher.games], [
                f"web:{job.id}:1", f"web:{job.id}:2", f"web:{job.id}:3"
            ])
            self.assertTrue(all(events[-1]["game_complete"] for _, events in publisher.games))
            snapshot = store.get_job(job.id)
            self.assertEqual((snapshot.total_games, snapshot.analyzing_games, snapshot.failed_games), (4, 3, 1))
            self.assertEqual(snapshot.status, "live")
            self.assertFalse(path.exists())

    def test_publish_failure_schedules_retry_and_retains_spool(self):
        with tempfile.TemporaryDirectory() as directory:
            store, job, path = self.make_job(directory, "[Result \"*\"]\n\n1. e4 *\n")
            worker = IngestionWorker(
                store,
                RecordingPublisher(fail=True),
                worker_id="worker-a",
                retry_delay=timedelta(seconds=30),
            )

            self.assertTrue(worker.process_one(NOW))

            self.assertEqual(store.get_job(job.id).status, "queued")
            self.assertTrue(path.exists())

    def test_empty_source_fails_job_and_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            store, job, path = self.make_job(directory, "")
            worker = IngestionWorker(store, RecordingPublisher(), worker_id="worker-a")

            self.assertTrue(worker.process_one(NOW))

            self.assertEqual(store.get_job(job.id).status, "failed")
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()

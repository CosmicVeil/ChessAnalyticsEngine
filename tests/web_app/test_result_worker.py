"""Joined-result commit ordering and replay behavior."""

import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.web_app.fakes import FakeAnalysisStore
from webapp.domain import ParsedGame, SpooledSource
from webapp.result_worker import ResultDisposition, ResultWorker


class FakeMessage:
    def __init__(self, payload, *, offset=3):
        self._payload = payload
        self._offset = offset

    def value(self):
        return json.dumps(self._payload).encode()

    def topic(self):
        return "chess-predictions"

    def partition(self):
        return 0

    def offset(self):
        return self._offset


class FakeConsumer:
    def __init__(self):
        self.committed = []

    def commit(self, message):
        self.committed.append(message)


def result_payload(game_id):
    return {
        "game_id": game_id,
        "pytorch_score": 0.81,
        "xgboost_score": 0.67,
        "pytorch_prediction": 1,
        "xgboost_prediction": 1,
        "pytorch_accuracy": None,
    }


class ResultWorkerTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeAnalysisStore()
        source = SpooledSource("paste", "pasted-pgn", Path("/tmp/pgn"))
        self.job = self.store.create_queued_job(source)
        self.store.claim_next_job(
            "worker-a",
            datetime(2026, 8, 4, tzinfo=UTC),
            timedelta(minutes=5),
        )
        self.game = self.store.upsert_parsed_game(self.job.id, ParsedGame(sequence=1))
        self.store.mark_game_published(self.game.id)
        self.consumer = FakeConsumer()
        self.worker = ResultWorker(self.store, self.consumer)

    def test_known_result_is_stored_before_commit_and_replay_is_idempotent(self):
        first = FakeMessage(result_payload(self.game.pipeline_game_id))
        replay = FakeMessage(result_payload(self.game.pipeline_game_id), offset=4)

        self.assertIs(self.worker.process(first), ResultDisposition.STORED)
        event_count = len(self.store.events)
        self.assertIs(self.worker.process(replay), ResultDisposition.REPLAYED)

        self.assertEqual(self.consumer.committed, [first, replay])
        self.assertEqual(len(self.store.events), event_count)

    def test_unknown_web_result_is_persisted_then_committed(self):
        message = FakeMessage(result_payload("web:missing:1"))
        self.assertIs(self.worker.process(message), ResultDisposition.UNMATCHED)
        self.assertEqual(len(self.store.unmatched_results), 1)
        self.assertEqual(self.consumer.committed, [message])

    def test_non_web_result_is_ignored_and_committed(self):
        message = FakeMessage(result_payload("simulator Clean"))
        self.assertIs(self.worker.process(message), ResultDisposition.IGNORED)
        self.assertEqual(self.consumer.committed, [message])

    def test_database_error_does_not_commit(self):
        class BrokenStore:
            def store_joined_result(self, result):
                raise RuntimeError("database down")

        consumer = FakeConsumer()
        worker = ResultWorker(BrokenStore(), consumer)
        with self.assertRaisesRegex(RuntimeError, "database down"):
            worker.process(FakeMessage(result_payload("web:job:1")))
        self.assertEqual(consumer.committed, [])


if __name__ == "__main__":
    unittest.main()

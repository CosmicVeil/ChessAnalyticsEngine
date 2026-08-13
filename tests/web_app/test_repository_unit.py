"""Unit contracts shared by repository, API, workers, and SSE."""

from __future__ import annotations

import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from webapp.domain import (
    AnalysisStore,
    GameQuery,
    GameSnapshot,
    JobSnapshot,
    JoinedResult,
    Page,
    ParsedGame,
    SpooledSource,
    StoreResultOutcome,
)
from tests.web_app.fakes import FakeAnalysisStore


class SnapshotViewTests(unittest.TestCase):
    """Catch drift in the durable browser payload contract."""

    def setUp(self) -> None:
        self.job_id = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        self.game_id = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
        self.created_at = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
        self.updated_at = datetime(2026, 8, 4, 12, 5, tzinfo=UTC)

    def test_job_view_contains_only_the_shared_public_shape(self) -> None:
        snapshot = JobSnapshot(
            id=self.job_id,
            source_type="file",
            source_name="round-one.pgn",
            status="live",
            total_games=4,
            queued_games=1,
            analyzing_games=1,
            completed_games=1,
            failed_games=1,
            created_at=self.created_at,
            updated_at=self.updated_at,
            completed_at=None,
        )

        self.assertEqual(
            snapshot.to_view(),
            {
                "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "source_type": "file",
                "source_name": "round-one.pgn",
                "status": "live",
                "counts": {
                    "total": 4,
                    "queued": 1,
                    "analyzing": 1,
                    "completed": 1,
                    "failed": 1,
                },
                "created_at": "2026-08-04T12:00:00+00:00",
                "updated_at": "2026-08-04T12:05:00+00:00",
                "completed_at": None,
            },
        )

    def test_game_view_contains_nested_players_error_and_scores(self) -> None:
        snapshot = GameSnapshot(
            id=self.game_id,
            job_id=self.job_id,
            sequence=2,
            pipeline_game_id=f"web:{self.job_id}:2",
            status="completed",
            white_name="Ada",
            black_name="Grace",
            white_elo=2210,
            black_elo=2190,
            result="1-0",
            played_at=self.created_at,
            opening="Sicilian Defense",
            error_code=None,
            error_message=None,
            pytorch_score=0.8437,
            xgboost_score=0.7161,
            pytorch_prediction=True,
            xgboost_prediction=True,
        )

        self.assertEqual(
            snapshot.to_view(),
            {
                "id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                "sequence": 2,
                "status": "completed",
                "white": {"name": "Ada", "elo": 2210},
                "black": {"name": "Grace", "elo": 2190},
                "result": "1-0",
                "played_at": "2026-08-04T12:00:00+00:00",
                "opening": "Sicilian Defense",
                "error": None,
                "scores": {
                    "pytorch": {"score": 0.8437, "prediction": True},
                    "xgboost": {"score": 0.7161, "prediction": True},
                    "agreement": True,
                },
            },
        )

    def test_failed_and_pending_game_views_are_unambiguous(self) -> None:
        failed = GameSnapshot(
            id=self.game_id,
            job_id=self.job_id,
            sequence=1,
            pipeline_game_id=f"web:{self.job_id}:1",
            status="failed",
            error_code="invalid_pgn",
            error_message="illegal move",
        )
        pending = GameSnapshot(
            id=uuid.uuid4(),
            job_id=self.job_id,
            sequence=2,
            pipeline_game_id=f"web:{self.job_id}:2",
            status="queued",
        )

        self.assertEqual(
            failed.to_view()["error"],
            {"code": "invalid_pgn", "message": "illegal move"},
        )
        self.assertIsNone(failed.to_view()["scores"])
        self.assertIsNone(pending.to_view()["error"])
        self.assertIsNone(pending.to_view()["scores"])


class QueryContractTests(unittest.TestCase):
    def test_game_query_defaults_are_bounded_and_page_is_generic(self) -> None:
        query = GameQuery()
        page = Page(items=("first", "second"), next_cursor=2, has_more=True)

        self.assertEqual(query.status, "all")
        self.assertEqual(query.agreement, "all")
        self.assertIsNone(query.score_min)
        self.assertIsNone(query.score_max)
        self.assertIsNone(query.cursor)
        self.assertEqual(query.limit, 50)
        self.assertEqual(page.items, ("first", "second"))

    def test_analysis_store_protocol_is_runtime_checkable(self) -> None:
        class MissingStoreMethods:
            pass

        self.assertFalse(isinstance(MissingStoreMethods(), AnalysisStore))


class FakeStoreContractTests(unittest.TestCase):
    @staticmethod
    def _live_game(store: FakeAnalysisStore):
        source = SpooledSource("file", "batch.pgn", Path("/spool/batch"))
        now = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
        job = store.create_queued_job(source)
        store.claim_next_job("worker-a", now, timedelta(minutes=5))
        game = store.upsert_parsed_game(job.id, ParsedGame(sequence=1))
        store.mark_game_published(game.id)
        return job, game

    @staticmethod
    def _result(pipeline_game_id: str) -> JoinedResult:
        return JoinedResult(
            pipeline_game_id=pipeline_game_id,
            pytorch_score=0.8,
            xgboost_score=0.4,
            pytorch_prediction=True,
            xgboost_prediction=False,
        )

    def test_job_status_contract_uses_live_while_game_status_keeps_analyzing(
        self,
    ) -> None:
        from webapp.models import (
            GAME_STATUSES,
            JOB_STATUSES,
            JOB_STATUS_LIVE,
        )

        self.assertEqual(JOB_STATUS_LIVE, "live")
        self.assertEqual(
            JOB_STATUSES,
            (
                "received",
                "queued",
                "parsing",
                "live",
                "completed",
                "partial",
                "failed",
            ),
        )
        self.assertEqual(
            GAME_STATUSES,
            ("queued", "analyzing", "completed", "failed"),
        )

    def test_fake_supports_protocol_job_claim_and_event_flow(self) -> None:
        store = FakeAnalysisStore()
        source = SpooledSource("file", "batch.pgn", Path("/spool/batch"))
        now = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)

        job = store.create_queued_job(source)
        claimed = store.claim_next_job("worker-a", now, timedelta(minutes=5))

        self.assertIsInstance(store, AnalysisStore)
        self.assertEqual(claimed.id, job.id)
        self.assertEqual(store.get_job(job.id).status, "parsing")
        self.assertEqual(
            [event.event_type for event in store.events_after(job.id, 0, 10)],
            ["job.updated", "job.updated"],
        )
        self.assertEqual(store.latest_event_id(job.id), store.events[-1].id)

    def test_fake_latest_event_cursor_is_zero_without_job_events(self) -> None:
        store = FakeAnalysisStore()

        self.assertEqual(store.latest_event_id(uuid.uuid4()), 0)

    def test_completed_game_is_visible_live_before_ingestion_reaches_eof(
        self,
    ) -> None:
        store = FakeAnalysisStore()
        job, game = self._live_game(store)

        self.assertEqual(store.get_job(job.id).status, "live")
        outcome = store.store_joined_result(
            self._result(game.pipeline_game_id)
        )
        snapshot = store.get_job(job.id)

        self.assertIs(outcome, StoreResultOutcome.STORED)
        self.assertEqual(snapshot.status, "live")
        self.assertEqual(snapshot.completed_games, 1)
        self.assertEqual(snapshot.analyzing_games, 0)
        self.assertIsNone(snapshot.completed_at)

    def test_eof_waits_for_the_last_discovered_game_then_terminalizes(
        self,
    ) -> None:
        store = FakeAnalysisStore()
        job, first = self._live_game(store)
        store.store_joined_result(self._result(first.pipeline_game_id))
        second = store.upsert_parsed_game(job.id, ParsedGame(sequence=2))
        store.mark_game_published(second.id)

        store.finish_ingestion(job.id, "worker-a")

        waiting = store.get_job(job.id)
        self.assertEqual(waiting.status, "live")
        self.assertIsNone(waiting.completed_at)

        store.store_joined_result(self._result(second.pipeline_game_id))

        completed = store.get_job(job.id)
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.completed_games, 2)
        self.assertIsNotNone(completed.completed_at)


if __name__ == "__main__":
    unittest.main()

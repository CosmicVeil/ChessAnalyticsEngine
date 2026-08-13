"""PostgreSQL integration tests for transactional analysis storage."""

from __future__ import annotations

import os
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from webapp.domain import (
    GameQuery,
    InvalidGame,
    JoinedResult,
    ParsedGame,
    SpooledSource,
    StoreResultOutcome,
)
from webapp.models import AnalysisGame, AnalysisJob, AnalysisResult, UnmatchedResult
from webapp.repository import SqlAlchemyAnalysisStore


POSTGRES_REQUIRED = "PostgreSQL integration test requires TEST_DATABASE_URL"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


class FakeKafkaMessage:
    """Complete Kafka-coordinate surface consumed by the repository."""

    def __init__(
        self,
        *,
        topic: str = "chess-predictions",
        partition: int = 2,
        offset: int = 19,
        pipeline_game_id: str = "web:missing:1",
    ) -> None:
        self._topic = topic
        self._partition = partition
        self._offset = offset
        self.pipeline_game_id = pipeline_game_id

    def topic(self) -> str:
        return self._topic

    def partition(self) -> int:
        return self._partition

    def offset(self) -> int:
        return self._offset


class RepositoryPostgresTests(unittest.TestCase):
    """Exercise real transactions, locks, constraints, and JSONB payloads."""

    engine: Engine
    sessions: sessionmaker[Session]
    store: SqlAlchemyAnalysisStore

    @classmethod
    def setUpClass(cls) -> None:
        database_url = os.environ.get("TEST_DATABASE_URL", "").strip()
        if not database_url:
            raise unittest.SkipTest(POSTGRES_REQUIRED)
        if make_url(database_url).get_backend_name() != "postgresql":
            raise AssertionError("TEST_DATABASE_URL must use PostgreSQL")

        config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", database_url)
        command.upgrade(config, "head")
        cls.engine = sa.create_engine(database_url, pool_pre_ping=True)
        cls.sessions = sessionmaker(
            bind=cls.engine,
            class_=Session,
            expire_on_commit=False,
        )
        cls.store = SqlAlchemyAnalysisStore(cls.sessions)

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "engine"):
            cls.engine.dispose()

    def setUp(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(sa.text(
                "TRUNCATE job_events, analysis_results, analysis_games, "
                "unmatched_results, analysis_jobs RESTART IDENTITY CASCADE"
            ))

    @staticmethod
    def source(name: str = "batch.pgn") -> SpooledSource:
        return SpooledSource(
            source_type="file",
            source_name=name,
            spool_path=Path(f"/private/spool/{name}"),
        )

    @staticmethod
    def parsed(sequence: int, *, white_name: str = "Ada") -> ParsedGame:
        return ParsedGame(
            sequence=sequence,
            white_name=white_name,
            black_name="Grace",
            white_elo=2210,
            black_elo=2190,
            result="1-0",
            played_at=NOW,
            opening="Sicilian Defense",
        )

    @staticmethod
    def result(pipeline_game_id: str) -> JoinedResult:
        return JoinedResult(
            pipeline_game_id=pipeline_game_id,
            pytorch_score=0.8437,
            xgboost_score=0.7161,
            pytorch_prediction=True,
            xgboost_prediction=True,
            model_payload={"schema": "joined-v1"},
        )

    def claim(self, job_id: uuid.UUID, worker_id: str = "worker-a"):
        claimed = self.store.claim_next_job(
            worker_id,
            NOW,
            timedelta(minutes=5),
        )
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, job_id)
        return claimed

    def test_create_job_and_initial_event_commit_one_public_snapshot(self) -> None:
        job = self.store.create_queued_job(self.source())

        persisted = self.store.get_job(job.id)
        events = self.store.events_after(job.id, event_id=0, limit=10)
        self.assertEqual(persisted, job)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "job.updated")
        self.assertEqual(events[0].payload, {"job": job.to_view()})
        self.assertNotIn("spool_path", events[0].payload["job"])
        self.assertEqual(self.store.latest_event_id(job.id), events[0].id)
        self.assertEqual(self.store.latest_event_id(uuid.uuid4()), 0)

    def test_claim_skips_a_locked_oldest_job(self) -> None:
        first = self.store.create_queued_job(self.source("first.pgn"))
        second = self.store.create_queued_job(self.source("second.pgn"))
        with self.engine.begin() as connection:
            connection.execute(
                sa.text(
                    "UPDATE analysis_jobs SET created_at = :created_at WHERE id = :id"
                ),
                {"created_at": NOW - timedelta(hours=1), "id": first.id},
            )
            connection.execute(
                sa.text(
                    "UPDATE analysis_jobs SET created_at = :created_at WHERE id = :id"
                ),
                {"created_at": NOW, "id": second.id},
            )

        locking_session = self.sessions()
        try:
            locking_session.begin()
            locking_session.execute(
                sa.select(AnalysisJob)
                .where(AnalysisJob.id == first.id)
                .with_for_update()
            ).scalar_one()

            claimed = self.store.claim_next_job(
                "worker-b",
                NOW + timedelta(minutes=1),
                timedelta(minutes=5),
            )
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.id, second.id)
        finally:
            locking_session.rollback()
            locking_session.close()

    def test_expired_claim_is_recovered_but_live_claim_is_not_stolen(self) -> None:
        job = self.store.create_queued_job(self.source())
        first_claim = self.store.claim_next_job(
            "worker-a", NOW, timedelta(minutes=5)
        )

        live_attempt = self.store.claim_next_job(
            "worker-b", NOW + timedelta(minutes=4), timedelta(minutes=5)
        )
        recovered = self.store.claim_next_job(
            "worker-b", NOW + timedelta(minutes=6), timedelta(minutes=5)
        )

        self.assertEqual(first_claim.id, job.id)
        self.assertIsNone(live_attempt)
        self.assertEqual(recovered.id, job.id)
        self.assertEqual(recovered.attempt_count, 2)
        self.assertEqual(recovered.claimed_by, "worker-b")

    def test_renew_claim_requires_the_current_owner(self) -> None:
        job = self.store.create_queued_job(self.source())
        self.claim(job.id)

        self.assertFalse(
            self.store.renew_claim(
                job.id,
                "worker-b",
                NOW + timedelta(minutes=1),
                timedelta(minutes=5),
            )
        )
        self.assertTrue(
            self.store.renew_claim(
                job.id,
                "worker-a",
                NOW + timedelta(minutes=1),
                timedelta(minutes=5),
            )
        )

    def test_sequence_upsert_and_publish_are_idempotent(self) -> None:
        job = self.store.create_queued_job(self.source())
        self.claim(job.id)

        first = self.store.upsert_parsed_game(job.id, self.parsed(1))
        events_after_first_upsert = self.store.events_after(job.id, 0, 100)
        replay = self.store.upsert_parsed_game(
            job.id, self.parsed(1, white_name="Changed on replay")
        )
        self.store.mark_game_published(first.id)
        after_publish = self.store.get_job(job.id)
        self.store.mark_game_published(first.id)
        after_replayed_publish = self.store.get_job(job.id)

        self.assertEqual(replay.id, first.id)
        self.assertEqual(replay.pipeline_game_id, f"web:{job.id}:1")
        self.assertEqual(len(self.store.list_games(job.id, GameQuery()).items), 1)
        self.assertEqual(
            len(self.store.events_after(job.id, 0, 100)),
            len(events_after_first_upsert) + 1,
        )
        self.assertEqual(after_publish.total_games, 1)
        self.assertEqual(after_publish.queued_games, 0)
        self.assertEqual(after_publish.analyzing_games, 1)
        self.assertEqual(after_replayed_publish, after_publish)

    def test_replayed_joined_result_changes_counters_and_events_once(self) -> None:
        job = self.store.create_queued_job(self.source())
        self.claim(job.id)
        game = self.store.upsert_parsed_game(job.id, self.parsed(1))
        self.store.mark_game_published(game.id)
        event_count_before = len(self.store.events_after(job.id, 0, 100))

        inserted = self.store.store_joined_result(
            self.result(game.pipeline_game_id)
        )
        event_count_after_insert = len(self.store.events_after(job.id, 0, 100))
        replayed = self.store.store_joined_result(
            self.result(game.pipeline_game_id)
        )

        snapshot = self.store.get_job(job.id)
        self.assertIs(inserted, StoreResultOutcome.STORED)
        self.assertIs(replayed, StoreResultOutcome.REPLAYED)
        self.assertEqual(snapshot.status, "live")
        self.assertIsNone(snapshot.completed_at)
        self.assertEqual(snapshot.analyzing_games, 0)
        self.assertEqual(snapshot.completed_games, 1)
        self.assertEqual(event_count_after_insert, event_count_before + 1)
        self.assertEqual(
            len(self.store.events_after(job.id, 0, 100)),
            event_count_after_insert,
        )
        with self.sessions() as session:
            self.assertEqual(session.scalar(sa.select(sa.func.count(AnalysisResult.game_id))), 1)

    def test_result_transaction_emits_exact_game_event_shape(self) -> None:
        job = self.store.create_queued_job(self.source())
        self.claim(job.id)
        game = self.store.upsert_parsed_game(job.id, self.parsed(1))
        self.store.mark_game_published(game.id)

        self.store.store_joined_result(self.result(game.pipeline_game_id))

        event = self.store.events_after(job.id, 0, 100)[-1]
        stored_game = self.store.list_games(job.id, GameQuery()).items[0]
        stored_job = self.store.get_job(job.id)
        self.assertEqual(event.event_type, "game.updated")
        self.assertEqual(
            event.payload,
            {"job": stored_job.to_view(), "game": stored_game.to_view()},
        )
        self.assertEqual(set(event.payload), {"job", "game"})

    def test_failed_completed_and_partial_terminal_states_use_all_counters(self) -> None:
        failed_job = self.store.create_queued_job(self.source("failed.pgn"))
        self.claim(failed_job.id)
        self.store.record_invalid_game(
            failed_job.id,
            InvalidGame(1, "invalid_pgn", "illegal move"),
        )
        self.store.finish_ingestion(failed_job.id, "worker-a")

        partial_job = self.store.create_queued_job(self.source("partial.pgn"))
        self.claim(partial_job.id)
        self.store.record_invalid_game(
            partial_job.id,
            InvalidGame(1, "invalid_pgn", "illegal move"),
        )
        valid = self.store.upsert_parsed_game(partial_job.id, self.parsed(2))
        self.store.mark_game_published(valid.id)
        self.store.store_joined_result(self.result(valid.pipeline_game_id))
        self.store.finish_ingestion(partial_job.id, "worker-a")

        complete_job = self.store.create_queued_job(self.source("complete.pgn"))
        self.claim(complete_job.id)
        complete_game = self.store.upsert_parsed_game(
            complete_job.id, self.parsed(1)
        )
        self.store.mark_game_published(complete_game.id)
        self.store.store_joined_result(self.result(complete_game.pipeline_game_id))
        self.store.finish_ingestion(complete_job.id, "worker-a")

        failed = self.store.get_job(failed_job.id)
        partial = self.store.get_job(partial_job.id)
        complete = self.store.get_job(complete_job.id)
        self.assertEqual((failed.status, failed.completed_games, failed.failed_games), ("failed", 0, 1))
        self.assertEqual((partial.status, partial.completed_games, partial.failed_games), ("partial", 1, 1))
        self.assertEqual((complete.status, complete.completed_games, complete.failed_games), ("completed", 1, 0))
        self.assertIsNotNone(failed.completed_at)
        self.assertIsNotNone(partial.completed_at)
        self.assertIsNotNone(complete.completed_at)

    def test_finish_ingestion_terminalizes_a_fast_result(self) -> None:
        job = self.store.create_queued_job(self.source())
        self.claim(job.id)
        game = self.store.upsert_parsed_game(job.id, self.parsed(1))
        self.store.mark_game_published(game.id)
        self.store.store_joined_result(self.result(game.pipeline_game_id))
        in_flight = self.store.get_job(job.id)

        self.store.finish_ingestion(job.id, "worker-a")

        finished = self.store.get_job(job.id)
        self.assertEqual(in_flight.status, "live")
        self.assertIsNone(in_flight.completed_at)
        self.assertEqual(finished.status, "completed")
        self.assertIsNotNone(finished.completed_at)

    def test_later_sequence_keeps_a_streaming_job_open_after_fast_result(self) -> None:
        valid_job = self.store.create_queued_job(self.source("valid-late.pgn"))
        self.claim(valid_job.id)
        first = self.store.upsert_parsed_game(valid_job.id, self.parsed(1))
        self.store.mark_game_published(first.id)
        self.store.store_joined_result(self.result(first.pipeline_game_id))
        self.assertEqual(self.store.get_job(valid_job.id).status, "live")

        second = self.store.upsert_parsed_game(valid_job.id, self.parsed(2))
        reopened = self.store.get_job(valid_job.id)
        self.assertEqual(reopened.status, "live")
        self.assertIsNone(reopened.completed_at)
        self.assertEqual(
            (
                reopened.total_games,
                reopened.queued_games,
                reopened.analyzing_games,
                reopened.completed_games,
                reopened.failed_games,
            ),
            (2, 1, 0, 1, 0),
        )

        self.store.mark_game_published(second.id)
        self.assertEqual(self.store.get_job(valid_job.id).status, "live")
        self.store.store_joined_result(self.result(second.pipeline_game_id))
        self.assertEqual(self.store.get_job(valid_job.id).status, "live")
        self.store.finish_ingestion(valid_job.id, "worker-a")
        completed = self.store.get_job(valid_job.id)
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.completed_games, 2)

        invalid_job = self.store.create_queued_job(
            self.source("invalid-late.pgn")
        )
        self.claim(invalid_job.id)
        only_valid = self.store.upsert_parsed_game(invalid_job.id, self.parsed(1))
        self.store.mark_game_published(only_valid.id)
        self.store.store_joined_result(self.result(only_valid.pipeline_game_id))
        self.assertEqual(self.store.get_job(invalid_job.id).status, "live")

        self.store.record_invalid_game(
            invalid_job.id,
            InvalidGame(2, "invalid_pgn", "late illegal move"),
        )
        reopened_after_invalid = self.store.get_job(invalid_job.id)
        self.assertEqual(reopened_after_invalid.status, "live")
        self.assertIsNone(reopened_after_invalid.completed_at)
        self.store.finish_ingestion(invalid_job.id, "worker-a")
        self.assertEqual(self.store.get_job(invalid_job.id).status, "partial")

    def test_events_are_increasing_resumable_and_use_only_shared_types(self) -> None:
        job = self.store.create_queued_job(self.source())
        self.claim(job.id)
        game = self.store.upsert_parsed_game(job.id, self.parsed(1))
        self.store.mark_game_published(game.id)
        self.store.store_joined_result(self.result(game.pipeline_game_id))

        all_events = self.store.events_after(job.id, 0, 100)
        first_page = self.store.events_after(job.id, 0, 2)
        resumed = self.store.events_after(job.id, first_page[-1].id, 100)

        ids = [event.id for event in all_events]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(first_page + resumed, all_events)
        self.assertEqual(
            {event.event_type for event in all_events},
            {"job.updated", "game.updated"},
        )
        self.assertEqual(self.store.latest_event_id(job.id), all_events[-1].id)

    def test_unmatched_result_source_coordinates_are_idempotent(self) -> None:
        message = FakeKafkaMessage()
        payload = {"game_id": message.pipeline_game_id, "joined": True}

        self.store.store_unmatched_result(message, payload)
        self.store.store_unmatched_result(message, payload)

        with self.sessions() as session:
            rows = session.scalars(sa.select(UnmatchedResult)).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].payload, payload)


if __name__ == "__main__":
    unittest.main()

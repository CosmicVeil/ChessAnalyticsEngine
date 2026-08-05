"""End-to-end adapter flow against real PostgreSQL transactions."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from webapp import create_app
from webapp.config import Settings
from webapp.domain import WebDependencies
from webapp.ingestion_worker import IngestionWorker
from webapp.repository import SqlAlchemyAnalysisStore
from webapp.result_worker import ResultDisposition, ResultWorker
from webapp.spool import SpoolService
from webapp.sse import stream_events


POSTGRES_REQUIRED = "PostgreSQL integration test requires TEST_DATABASE_URL"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPOSITORY_ROOT / "tests/fixtures/webapp/small_batch.pgn"
NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


class HealthyProbe:
    def check(self) -> dict[str, str]:
        return {"database": "ok", "kafka": "ok"}


class JoinedMessage:
    def __init__(self, game_id: str, offset: int) -> None:
        sequence = int(game_id.rsplit(":", 1)[-1])
        self.payload = {
            "game_id": game_id,
            "pytorch_score": 0.70 + sequence / 100,
            "xgboost_score": 0.60 + sequence / 100,
            "pytorch_prediction": sequence != 3,
            "xgboost_prediction": sequence == 1,
            "schema": "joined-v1",
        }
        self._offset = offset

    def value(self) -> bytes:
        return json.dumps(self.payload).encode()

    def topic(self) -> str:
        return "chess-predictions"

    def partition(self) -> int:
        return 0

    def offset(self) -> int:
        return self._offset


class RecordingConsumer:
    def __init__(self) -> None:
        self.committed: list[JoinedMessage] = []

    def commit(self, message: JoinedMessage) -> None:
        self.committed.append(message)


class ImmediateResultPublisher:
    """Simulate a fast pipeline result arriving during PGN streaming."""

    def __init__(self, result_worker: ResultWorker) -> None:
        self.result_worker = result_worker
        self.messages: list[JoinedMessage] = []

    def publish_game(self, game_id: str, events: object) -> None:
        materialized = list(events)
        if not materialized or not materialized[-1]["game_complete"]:
            raise AssertionError("each published game needs a completion boundary")
        message = JoinedMessage(game_id, len(self.messages))
        self.messages.append(message)
        outcome = self.result_worker.process(message)
        if outcome is not ResultDisposition.STORED:
            raise AssertionError(f"unexpected result disposition: {outcome}")


class AdapterIntegrationPostgresTests(unittest.TestCase):
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
        cls.database_url = database_url

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

    def test_submission_streaming_results_replay_and_sse_are_one_flow(self) -> None:
        with tempfile.TemporaryDirectory() as spool_directory:
            settings = Settings(
                database_url=self.database_url,
                pgn_spool_directory=Path(spool_directory),
                max_pgn_upload_bytes=1024 * 1024,
                max_pasted_pgn_bytes=1024 * 1024,
                secret_key="integration-test",
            )
            spool = SpoolService(
                Path(spool_directory),
                max_pgn_upload_bytes=settings.max_pgn_upload_bytes,
                max_pasted_pgn_bytes=settings.max_pasted_pgn_bytes,
            )
            app = create_app(
                settings,
                WebDependencies(self.store, spool, HealthyProbe()),
            )
            client = app.test_client()

            response = client.post(
                "/api/analyses",
                data={"pgn_text": FIXTURE.read_text(encoding="utf-8")},
            )
            self.assertEqual(response.status_code, 202)
            job_id = uuid.UUID(response.get_json()["job"]["id"])

            consumer = RecordingConsumer()
            result_worker = ResultWorker(self.store, consumer)
            publisher = ImmediateResultPublisher(result_worker)
            ingestion_worker = IngestionWorker(
                self.store,
                publisher,
                worker_id="integration-ingest",
            )
            self.assertTrue(ingestion_worker.process_one(NOW))

            job = self.store.get_job(job_id)
            self.assertEqual(job.status, "partial")
            self.assertEqual(
                (job.total_games, job.completed_games, job.failed_games),
                (4, 3, 1),
            )
            self.assertEqual(
                [message.payload["game_id"] for message in publisher.messages],
                [f"web:{job_id}:{sequence}" for sequence in (1, 2, 3)],
            )

            event_count = len(self.store.events_after(job_id, 0, 1000))
            replay = JoinedMessage(f"web:{job_id}:1", 99)
            self.assertIs(
                result_worker.process(replay),
                ResultDisposition.REPLAYED,
            )
            self.assertEqual(
                len(self.store.events_after(job_id, 0, 1000)),
                event_count,
            )

            games_response = client.get(
                f"/api/analyses/{job_id}/games?limit=50"
            )
            self.assertEqual(games_response.status_code, 200)
            games = games_response.get_json()["items"]
            self.assertEqual([game["sequence"] for game in games], [1, 2, 3, 4])
            self.assertEqual(
                [game["status"] for game in games],
                ["completed", "completed", "completed", "failed"],
            )
            self.assertTrue(all(game["scores"] for game in games[:3]))

            chunks = list(stream_events(self.store, job_id, wait=lambda: None))
            event_ids = [
                int(chunk.split(b"\n", 1)[0].removeprefix(b"id: "))
                for chunk in chunks
            ]
            self.assertEqual(event_ids, sorted(event_ids))
            completed_sequences = []
            for chunk in chunks:
                if b"event: game.updated\n" not in chunk:
                    continue
                data_line = next(
                    line for line in chunk.splitlines() if line.startswith(b"data: ")
                )
                payload = json.loads(data_line.removeprefix(b"data: "))
                if payload["game"]["status"] == "completed":
                    completed_sequences.append(payload["game"]["sequence"])
            self.assertEqual(completed_sequences, [1, 2, 3])
            self.assertEqual(len(consumer.committed), 4)
            self.assertEqual(list(Path(spool_directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()

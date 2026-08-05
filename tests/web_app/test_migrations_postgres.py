"""PostgreSQL integration tests for the analysis adapter migration."""

from __future__ import annotations

import os
import unittest
import uuid
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.exc import IntegrityError


POSTGRES_REQUIRED = "PostgreSQL integration test requires TEST_DATABASE_URL"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
REQUIRED_TABLES = {
    "analysis_jobs",
    "analysis_games",
    "analysis_results",
    "job_events",
    "unmatched_results",
}


class MigrationPostgresTests(unittest.TestCase):
    """Exercise the real migration and constraints on PostgreSQL."""

    engine: Engine
    database_url: str
    alembic_config: Config

    @classmethod
    def setUpClass(cls) -> None:
        cls.database_url = os.environ.get("TEST_DATABASE_URL", "").strip()
        if not cls.database_url:
            raise unittest.SkipTest(POSTGRES_REQUIRED)

        url = make_url(cls.database_url)
        if url.get_backend_name() != "postgresql":
            raise AssertionError("TEST_DATABASE_URL must use PostgreSQL")

        cls.engine = sa.create_engine(cls.database_url, pool_pre_ping=True)
        cls.alembic_config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
        cls.alembic_config.set_main_option("sqlalchemy.url", cls.database_url)
        command.upgrade(cls.alembic_config, "head")

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "engine"):
            cls.engine.dispose()

    @staticmethod
    def _columns_by_name(inspector: sa.Inspector, table: str) -> dict[str, Any]:
        return {column["name"]: column for column in inspector.get_columns(table)}

    @staticmethod
    def _insert_job(
        connection: Connection,
        *,
        job_id: uuid.UUID | None = None,
        **overrides: Any,
    ) -> uuid.UUID:
        resolved_job_id = job_id or uuid.uuid4()
        values: dict[str, Any] = {
            "id": resolved_job_id,
            "source_type": "file",
            "source_name": "batch.pgn",
            "status": "queued",
            "total_games": 0,
            "queued_games": 0,
            "analyzing_games": 0,
            "completed_games": 0,
            "failed_games": 0,
            "attempt_count": 0,
        }
        values.update(overrides)
        connection.execute(sa.text("""
            INSERT INTO analysis_jobs (
                id,
                source_type,
                source_name,
                status,
                total_games,
                queued_games,
                analyzing_games,
                completed_games,
                failed_games,
                attempt_count
            ) VALUES (
                :id,
                :source_type,
                :source_name,
                :status,
                :total_games,
                :queued_games,
                :analyzing_games,
                :completed_games,
                :failed_games,
                :attempt_count
            )
        """), values)
        return resolved_job_id

    @staticmethod
    def _insert_game(
        connection: Connection,
        job_id: uuid.UUID,
        *,
        game_id: uuid.UUID | None = None,
        sequence: int = 1,
        pipeline_game_id: str | None = None,
        status: str = "queued",
    ) -> uuid.UUID:
        resolved_game_id = game_id or uuid.uuid4()
        connection.execute(
            sa.text("""
                INSERT INTO analysis_games (
                    id, job_id, sequence, pipeline_game_id, status
                ) VALUES (
                    :id, :job_id, :sequence, :pipeline_game_id, :status
                )
            """),
            {
                "id": resolved_game_id,
                "job_id": job_id,
                "sequence": sequence,
                "pipeline_game_id": pipeline_game_id
                or f"web:{job_id}:{sequence}",
                "status": status,
            },
        )
        return resolved_game_id

    def test_upgrade_creates_required_tables_with_postgresql_types(self) -> None:
        inspector = sa.inspect(self.engine)

        self.assertTrue(REQUIRED_TABLES.issubset(inspector.get_table_names()))

        uuid_columns = {
            "analysis_jobs": {"id"},
            "analysis_games": {"id", "job_id"},
            "analysis_results": {"game_id"},
            "job_events": {"job_id", "game_id"},
            "unmatched_results": {"id"},
        }
        for table, expected_columns in uuid_columns.items():
            columns = self._columns_by_name(inspector, table)
            for column_name in expected_columns:
                with self.subTest(table=table, column=column_name):
                    column_type = columns[column_name]["type"]
                    self.assertIsInstance(column_type, sa.Uuid)
                    self.assertTrue(column_type.as_uuid)

        timestamp_columns = {
            "analysis_jobs": {
                "lease_expires_at",
                "created_at",
                "updated_at",
                "completed_at",
            },
            "analysis_games": {"played_at"},
            "analysis_results": {"created_at"},
            "job_events": {"created_at"},
            "unmatched_results": {"created_at"},
        }
        for table, expected_columns in timestamp_columns.items():
            columns = self._columns_by_name(inspector, table)
            for column_name in expected_columns:
                with self.subTest(table=table, column=column_name):
                    column_type = columns[column_name]["type"]
                    self.assertIsInstance(column_type, postgresql.TIMESTAMP)
                    self.assertTrue(column_type.timezone)

        jsonb_columns = {
            "analysis_results": "model_payload",
            "job_events": "payload",
            "unmatched_results": "payload",
        }
        for table, column_name in jsonb_columns.items():
            with self.subTest(table=table, column=column_name):
                column_type = self._columns_by_name(inspector, table)[column_name][
                    "type"
                ]
                self.assertIsInstance(column_type, postgresql.JSONB)

        event_id_type = self._columns_by_name(inspector, "job_events")["id"][
            "type"
        ]
        self.assertIsInstance(event_id_type, sa.BigInteger)

        for table in ("analysis_jobs", "analysis_games"):
            with self.subTest(table=table, column="status"):
                status_type = self._columns_by_name(inspector, table)["status"][
                    "type"
                ]
                self.assertIsInstance(status_type, sa.String)
                self.assertNotIsInstance(status_type, postgresql.ENUM)

    def test_primary_keys_uniques_and_indexes_match_the_adapter_contract(self) -> None:
        inspector = sa.inspect(self.engine)

        expected_primary_keys = {
            "analysis_jobs": ["id"],
            "analysis_games": ["id"],
            "analysis_results": ["game_id"],
            "job_events": ["id"],
            "unmatched_results": ["id"],
        }
        for table, expected_columns in expected_primary_keys.items():
            with self.subTest(table=table):
                self.assertEqual(
                    inspector.get_pk_constraint(table)["constrained_columns"],
                    expected_columns,
                )

        expected_uniques = {
            "analysis_games": {
                ("job_id", "sequence"),
                ("pipeline_game_id",),
            },
            "unmatched_results": {("topic", "partition", "offset")},
        }
        for table, required_uniques in expected_uniques.items():
            actual_uniques = {
                tuple(constraint["column_names"])
                for constraint in inspector.get_unique_constraints(table)
            }
            with self.subTest(table=table):
                self.assertTrue(required_uniques.issubset(actual_uniques))

        expected_indexes = {
            "analysis_jobs": {
                "ix_analysis_jobs_status": ("status",),
            },
            "analysis_games": {
                "ix_analysis_games_job_id_sequence": ("job_id", "sequence"),
            },
            "job_events": {
                "ix_job_events_job_id_id": ("job_id", "id"),
            },
            "unmatched_results": {
                "ix_unmatched_results_pipeline_game_id": ("pipeline_game_id",),
            },
        }
        for table, required_indexes in expected_indexes.items():
            actual_indexes = {
                index["name"]: tuple(index["column_names"])
                for index in inspector.get_indexes(table)
            }
            for name, expected_columns in required_indexes.items():
                with self.subTest(table=table, index=name):
                    self.assertEqual(actual_indexes.get(name), expected_columns)

    def test_database_rejects_duplicate_pipeline_game_ids(self) -> None:
        pipeline_game_id = f"web:{uuid.uuid4()}:shared"
        with self.engine.begin() as connection:
            first_job_id = self._insert_job(connection)
            self._insert_game(
                connection,
                first_job_id,
                pipeline_game_id=pipeline_game_id,
            )

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                second_job_id = self._insert_job(connection)
                self._insert_game(
                    connection,
                    second_job_id,
                    pipeline_game_id=pipeline_game_id,
                )

    def test_database_rejects_scores_outside_the_unit_interval(self) -> None:
        invalid_scores = (
            (-0.0001, 0.5),
            (1.0001, 0.5),
            (0.5, -0.0001),
            (0.5, 1.0001),
        )
        for sequence, (pytorch_score, xgboost_score) in enumerate(
            invalid_scores,
            start=1,
        ):
            with self.subTest(
                pytorch_score=pytorch_score,
                xgboost_score=xgboost_score,
            ):
                with self.assertRaises(IntegrityError):
                    with self.engine.begin() as connection:
                        job_id = self._insert_job(connection)
                        game_id = self._insert_game(
                            connection,
                            job_id,
                            sequence=sequence,
                        )
                        connection.execute(
                            sa.text("""
                                INSERT INTO analysis_results (
                                    game_id,
                                    pytorch_score,
                                    xgboost_score,
                                    pytorch_prediction,
                                    xgboost_prediction,
                                    model_payload
                                ) VALUES (
                                    :game_id,
                                    :pytorch_score,
                                    :xgboost_score,
                                    false,
                                    false,
                                    CAST('{}' AS JSONB)
                                )
                            """),
                            {
                                "game_id": game_id,
                                "pytorch_score": pytorch_score,
                                "xgboost_score": xgboost_score,
                            },
                        )

    def test_database_rejects_negative_job_counters(self) -> None:
        counter_columns = (
            "total_games",
            "queued_games",
            "analyzing_games",
            "completed_games",
            "failed_games",
            "attempt_count",
        )
        for counter_column in counter_columns:
            with self.subTest(counter=counter_column):
                with self.assertRaises(IntegrityError):
                    with self.engine.begin() as connection:
                        self._insert_job(connection, **{counter_column: -1})

    def test_database_rejects_unknown_job_and_game_statuses(self) -> None:
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                self._insert_job(connection, status="mystery")

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                job_id = self._insert_job(connection)
                self._insert_game(connection, job_id, status="mystery")

    def test_models_publish_checked_string_status_constants(self) -> None:
        from webapp.models import (
            GAME_STATUSES,
            JOB_STATUSES,
            AnalysisGame,
            AnalysisJob,
            AnalysisResult,
            Base,
            JobEvent,
            UnmatchedResult,
        )

        self.assertEqual(
            JOB_STATUSES,
            (
                "received",
                "queued",
                "parsing",
                "analyzing",
                "completed",
                "partial",
                "failed",
            ),
        )
        self.assertEqual(
            GAME_STATUSES,
            ("queued", "analyzing", "completed", "failed"),
        )
        self.assertEqual(
            {
                AnalysisJob.__tablename__,
                AnalysisGame.__tablename__,
                AnalysisResult.__tablename__,
                JobEvent.__tablename__,
                UnmatchedResult.__tablename__,
            },
            REQUIRED_TABLES,
        )
        self.assertEqual(set(Base.metadata.tables), REQUIRED_TABLES)

        job_checks = {
            constraint.name
            for constraint in AnalysisJob.__table__.constraints
            if isinstance(constraint, sa.CheckConstraint)
        }
        game_checks = {
            constraint.name
            for constraint in AnalysisGame.__table__.constraints
            if isinstance(constraint, sa.CheckConstraint)
        }
        self.assertIn("ck_analysis_jobs_status", job_checks)
        self.assertIn("ck_analysis_games_status", game_checks)

    def test_downgrade_and_reupgrade_restore_the_schema(self) -> None:
        try:
            command.downgrade(self.alembic_config, "base")
            downgraded_tables = set(sa.inspect(self.engine).get_table_names())
            self.assertTrue(REQUIRED_TABLES.isdisjoint(downgraded_tables))
        finally:
            command.upgrade(self.alembic_config, "head")

        upgraded_tables = set(sa.inspect(self.engine).get_table_names())
        self.assertTrue(REQUIRED_TABLES.issubset(upgraded_tables))


if __name__ == "__main__":
    unittest.main()

"""Create the durable analysis adapter schema.

Revision ID: 20260804_0001
Revises:
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260804_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create analysis jobs, games, results, events, and unmatched rows."""

    op.create_table(
        "analysis_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_type", sa.String(length=16), nullable=False),
        sa.Column("source_name", sa.String(length=255), nullable=False),
        sa.Column("spool_path", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'received'"),
            nullable=False,
        ),
        sa.Column(
            "total_games",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "queued_games",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "analyzing_games",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "completed_games",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "failed_games",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("claimed_by", sa.String(length=255), nullable=True),
        sa.Column(
            "lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.CheckConstraint(
            "status IN ('received', 'queued', 'parsing', 'analyzing', "
            "'completed', 'partial', 'failed')",
            name="ck_analysis_jobs_status",
        ),
        sa.CheckConstraint(
            "total_games >= 0",
            name="ck_analysis_jobs_total_games_non_negative",
        ),
        sa.CheckConstraint(
            "queued_games >= 0",
            name="ck_analysis_jobs_queued_games_non_negative",
        ),
        sa.CheckConstraint(
            "analyzing_games >= 0",
            name="ck_analysis_jobs_analyzing_games_non_negative",
        ),
        sa.CheckConstraint(
            "completed_games >= 0",
            name="ck_analysis_jobs_completed_games_non_negative",
        ),
        sa.CheckConstraint(
            "failed_games >= 0",
            name="ck_analysis_jobs_failed_games_non_negative",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_analysis_jobs_attempt_count_non_negative",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_analysis_jobs"),
    )
    op.create_index(
        "ix_analysis_jobs_status",
        "analysis_jobs",
        ["status"],
        unique=False,
    )

    op.create_table(
        "analysis_games",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column(
            "pipeline_game_id",
            sa.String(length=128),
            nullable=False,
        ),
        sa.Column("white_name", sa.String(length=255), nullable=True),
        sa.Column("black_name", sa.String(length=255), nullable=True),
        sa.Column("white_elo", sa.Integer(), nullable=True),
        sa.Column("black_elo", sa.Integer(), nullable=True),
        sa.Column("result", sa.String(length=16), nullable=True),
        sa.Column("played_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opening", sa.String(length=255), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'queued'"),
            nullable=False,
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'analyzing', 'completed', 'failed')",
            name="ck_analysis_games_status",
        ),
        sa.CheckConstraint(
            "sequence >= 1",
            name="ck_analysis_games_sequence_positive",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["analysis_jobs.id"],
            name="fk_analysis_games_job_id_analysis_jobs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_analysis_games"),
        sa.UniqueConstraint(
            "job_id",
            "sequence",
            name="uq_analysis_games_job_id_sequence",
        ),
        sa.UniqueConstraint(
            "pipeline_game_id",
            name="uq_analysis_games_pipeline_game_id",
        ),
    )
    op.create_index(
        "ix_analysis_games_job_id_sequence",
        "analysis_games",
        ["job_id", "sequence"],
        unique=False,
    )

    op.create_table(
        "analysis_results",
        sa.Column("game_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pytorch_score", sa.Float(), nullable=False),
        sa.Column("xgboost_score", sa.Float(), nullable=False),
        sa.Column("pytorch_prediction", sa.Boolean(), nullable=False),
        sa.Column("xgboost_prediction", sa.Boolean(), nullable=False),
        sa.Column(
            "model_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "0 <= pytorch_score AND pytorch_score <= 1",
            name="ck_analysis_results_pytorch_score_range",
        ),
        sa.CheckConstraint(
            "0 <= xgboost_score AND xgboost_score <= 1",
            name="ck_analysis_results_xgboost_score_range",
        ),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["analysis_games.id"],
            name="fk_analysis_results_game_id_analysis_games",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("game_id", name="pk_analysis_results"),
        sa.UniqueConstraint(
            "game_id",
            name="uq_analysis_results_game_id",
        ),
    )

    op.create_table(
        "job_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("game_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["analysis_games.id"],
            name="fk_job_events_game_id_analysis_games",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["analysis_jobs.id"],
            name="fk_job_events_job_id_analysis_jobs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_job_events"),
    )
    op.create_index(
        "ix_job_events_job_id_id",
        "job_events",
        ["job_id", "id"],
        unique=False,
    )

    op.create_table(
        "unmatched_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("topic", sa.String(length=255), nullable=False),
        sa.Column("partition", sa.Integer(), nullable=False),
        sa.Column("offset", sa.BigInteger(), nullable=False),
        sa.Column(
            "pipeline_game_id",
            sa.String(length=128),
            nullable=False,
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "partition >= 0",
            name="ck_unmatched_results_partition_non_negative",
        ),
        sa.CheckConstraint(
            '"offset" >= 0',
            name="ck_unmatched_results_offset_non_negative",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_unmatched_results"),
        sa.UniqueConstraint(
            "topic",
            "partition",
            "offset",
            name="uq_unmatched_results_source_coordinates",
        ),
    )
    op.create_index(
        "ix_unmatched_results_pipeline_game_id",
        "unmatched_results",
        ["pipeline_game_id"],
        unique=False,
    )


def downgrade() -> None:
    """Remove the analysis adapter schema."""

    op.drop_index(
        "ix_unmatched_results_pipeline_game_id",
        table_name="unmatched_results",
    )
    op.drop_table("unmatched_results")
    op.drop_index("ix_job_events_job_id_id", table_name="job_events")
    op.drop_table("job_events")
    op.drop_table("analysis_results")
    op.drop_index(
        "ix_analysis_games_job_id_sequence",
        table_name="analysis_games",
    )
    op.drop_table("analysis_games")
    op.drop_index("ix_analysis_jobs_status", table_name="analysis_jobs")
    op.drop_table("analysis_jobs")

"""SQLAlchemy models for durable web analysis state."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


JOB_STATUS_RECEIVED = "received"
JOB_STATUS_QUEUED = "queued"
JOB_STATUS_PARSING = "parsing"
JOB_STATUS_LIVE = "live"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_PARTIAL = "partial"
JOB_STATUS_FAILED = "failed"
JOB_STATUSES = (
    JOB_STATUS_RECEIVED,
    JOB_STATUS_QUEUED,
    JOB_STATUS_PARSING,
    JOB_STATUS_LIVE,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_PARTIAL,
    JOB_STATUS_FAILED,
)

GAME_STATUS_QUEUED = "queued"
GAME_STATUS_ANALYZING = "analyzing"
GAME_STATUS_COMPLETED = "completed"
GAME_STATUS_FAILED = "failed"
GAME_STATUSES = (
    GAME_STATUS_QUEUED,
    GAME_STATUS_ANALYZING,
    GAME_STATUS_COMPLETED,
    GAME_STATUS_FAILED,
)
ANALYSIS_ENGINES = ("nnue", "xgboost", "consensus")


def _quoted_values(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class Base(DeclarativeBase):
    """Declarative metadata root for the web adapter schema."""


class User(Base):
    """A local account used to protect private PGN analyses."""

    __tablename__ = "users"
    __table_args__ = (
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.CheckConstraint(
            f"preferred_engine IN ({_quoted_values(ANALYSIS_ENGINES)})",
            name="ck_users_preferred_engine",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(sa.String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    password_hash: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    preferred_engine: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="nnue", server_default=sa.text("'nnue'")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    jobs: Mapped[list[AnalysisJob]] = relationship(back_populates="owner")


class AnalysisJob(Base):
    """One accepted PGN source and its durable processing counters."""

    __tablename__ = "analysis_jobs"
    __table_args__ = (
        sa.CheckConstraint(
            f"status IN ({_quoted_values(JOB_STATUSES)})",
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
        sa.Index("ix_analysis_jobs_status", "status"),
        sa.Index("ix_analysis_jobs_owner_created", "owner_id", "created_at"),
        sa.CheckConstraint(
            f"engine IN ({_quoted_values(ANALYSIS_ENGINES)})",
            name="ck_analysis_jobs_engine",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )
    engine: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="nnue", server_default=sa.text("'nnue'")
    )
    source_type: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    source_name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    spool_path: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    status: Mapped[str] = mapped_column(
        sa.String(16),
        nullable=False,
        default=JOB_STATUS_RECEIVED,
        server_default=sa.text(f"'{JOB_STATUS_RECEIVED}'"),
    )
    total_games: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    queued_games: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    analyzing_games: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    completed_games: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    failed_games: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    claimed_by: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    error_code: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

    games: Mapped[list[AnalysisGame]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    events: Mapped[list[JobEvent]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    owner: Mapped[User | None] = relationship(back_populates="jobs")


class AnalysisGame(Base):
    """One source-ordered game correlated with the Kafka pipeline."""

    __tablename__ = "analysis_games"
    __table_args__ = (
        sa.UniqueConstraint(
            "job_id",
            "sequence",
            name="uq_analysis_games_job_id_sequence",
        ),
        sa.UniqueConstraint(
            "pipeline_game_id",
            name="uq_analysis_games_pipeline_game_id",
        ),
        sa.CheckConstraint(
            f"status IN ({_quoted_values(GAME_STATUSES)})",
            name="ck_analysis_games_status",
        ),
        sa.CheckConstraint(
            "sequence >= 1",
            name="ck_analysis_games_sequence_positive",
        ),
        sa.Index(
            "ix_analysis_games_job_id_sequence",
            "job_id",
            "sequence",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("analysis_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    pipeline_game_id: Mapped[str] = mapped_column(
        sa.String(128), nullable=False
    )
    white_name: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    black_name: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    white_elo: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    black_elo: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    result: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)
    played_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    opening: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    status: Mapped[str] = mapped_column(
        sa.String(16),
        nullable=False,
        default=GAME_STATUS_QUEUED,
        server_default=sa.text(f"'{GAME_STATUS_QUEUED}'"),
    )
    error_code: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    job: Mapped[AnalysisJob] = relationship(back_populates="games")
    analysis_result: Mapped[AnalysisResult | None] = relationship(
        back_populates="game",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    events: Mapped[list[JobEvent]] = relationship(back_populates="game")


class AnalysisResult(Base):
    """Joined PyTorch and XGBoost output for one analysis game."""

    __tablename__ = "analysis_results"
    __table_args__ = (
        sa.UniqueConstraint("game_id", name="uq_analysis_results_game_id"),
        sa.CheckConstraint(
            "0 <= pytorch_score AND pytorch_score <= 1",
            name="ck_analysis_results_pytorch_score_range",
        ),
        sa.CheckConstraint(
            "0 <= xgboost_score AND xgboost_score <= 1",
            name="ck_analysis_results_xgboost_score_range",
        ),
    )

    game_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("analysis_games.id", ondelete="CASCADE"),
        primary_key=True,
    )
    pytorch_score: Mapped[float] = mapped_column(sa.Float, nullable=False)
    xgboost_score: Mapped[float] = mapped_column(sa.Float, nullable=False)
    pytorch_prediction: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    xgboost_prediction: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    model_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=sa.text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    game: Mapped[AnalysisGame] = relationship(back_populates="analysis_result")


class JobEvent(Base):
    """Ordered durable browser event for one analysis job."""

    __tablename__ = "job_events"
    __table_args__ = (
        sa.Index("ix_job_events_job_id_id", "job_id", "id"),
    )

    id: Mapped[int] = mapped_column(
        sa.BigInteger, primary_key=True, autoincrement=True
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("analysis_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    game_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("analysis_games.id", ondelete="SET NULL"),
        nullable=True,
    )
    event_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=sa.text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    job: Mapped[AnalysisJob] = relationship(back_populates="events")
    game: Mapped[AnalysisGame | None] = relationship(back_populates="events")


class UnmatchedResult(Base):
    """Unknown web result retained before committing its Kafka offset."""

    __tablename__ = "unmatched_results"
    __table_args__ = (
        sa.UniqueConstraint(
            "topic",
            "partition",
            "offset",
            name="uq_unmatched_results_source_coordinates",
        ),
        sa.CheckConstraint(
            "partition >= 0",
            name="ck_unmatched_results_partition_non_negative",
        ),
        sa.CheckConstraint(
            '"offset" >= 0',
            name="ck_unmatched_results_offset_non_negative",
        ),
        sa.Index(
            "ix_unmatched_results_pipeline_game_id",
            "pipeline_game_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    topic: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    partition: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    offset: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    pipeline_game_id: Mapped[str] = mapped_column(
        sa.String(128), nullable=False
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=sa.text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


__all__ = [
    "ANALYSIS_ENGINES",
    "GAME_STATUSES",
    "GAME_STATUS_ANALYZING",
    "GAME_STATUS_COMPLETED",
    "GAME_STATUS_FAILED",
    "GAME_STATUS_QUEUED",
    "JOB_STATUSES",
    "JOB_STATUS_COMPLETED",
    "JOB_STATUS_FAILED",
    "JOB_STATUS_LIVE",
    "JOB_STATUS_PARSING",
    "JOB_STATUS_PARTIAL",
    "JOB_STATUS_QUEUED",
    "JOB_STATUS_RECEIVED",
    "AnalysisGame",
    "AnalysisJob",
    "AnalysisResult",
    "Base",
    "JobEvent",
    "UnmatchedResult",
    "User",
]

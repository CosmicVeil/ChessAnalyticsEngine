"""Add private accounts, job ownership, and per-job engine selection.

Revision ID: 20261116_0003
Revises: 20260805_0002
Create Date: 2026-11-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20261116_0003"
down_revision: str | None = "20260805_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("preferred_engine", sa.String(length=16), server_default="nnue", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("preferred_engine IN ('nnue', 'xgboost', 'consensus')", name="ck_users_preferred_engine"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.add_column("analysis_jobs", sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("analysis_jobs", sa.Column("engine", sa.String(length=16), server_default="nnue", nullable=False))
    op.create_foreign_key("fk_analysis_jobs_owner_id_users", "analysis_jobs", "users", ["owner_id"], ["id"], ondelete="CASCADE")
    op.create_check_constraint("ck_analysis_jobs_engine", "analysis_jobs", "engine IN ('nnue', 'xgboost', 'consensus')")
    op.create_index("ix_analysis_jobs_owner_created", "analysis_jobs", ["owner_id", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_analysis_jobs_owner_created", table_name="analysis_jobs")
    op.drop_constraint("ck_analysis_jobs_engine", "analysis_jobs", type_="check")
    op.drop_constraint("fk_analysis_jobs_owner_id_users", "analysis_jobs", type_="foreignkey")
    op.drop_column("analysis_jobs", "engine")
    op.drop_column("analysis_jobs", "owner_id")
    op.drop_table("users")

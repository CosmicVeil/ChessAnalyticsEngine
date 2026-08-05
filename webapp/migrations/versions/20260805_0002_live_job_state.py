"""Rename the active batch state from analyzing to live.

Revision ID: 20260805_0002
Revises: 20260804_0001
Create Date: 2026-08-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260805_0002"
down_revision: str | None = "20260804_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Convert in-flight jobs and enforce the new live status contract."""

    op.drop_constraint(
        "ck_analysis_jobs_status",
        "analysis_jobs",
        type_="check",
    )
    op.execute(
        sa.text(
            "UPDATE analysis_jobs SET status = 'live' "
            "WHERE status = 'analyzing'"
        )
    )
    op.create_check_constraint(
        "ck_analysis_jobs_status",
        "analysis_jobs",
        "status IN ('received', 'queued', 'parsing', 'live', "
        "'completed', 'partial', 'failed')",
    )


def downgrade() -> None:
    """Restore the former analyzing batch state."""

    op.drop_constraint(
        "ck_analysis_jobs_status",
        "analysis_jobs",
        type_="check",
    )
    op.execute(
        sa.text(
            "UPDATE analysis_jobs SET status = 'analyzing' "
            "WHERE status = 'live'"
        )
    )
    op.create_check_constraint(
        "ck_analysis_jobs_status",
        "analysis_jobs",
        "status IN ('received', 'queued', 'parsing', 'analyzing', "
        "'completed', 'partial', 'failed')",
    )

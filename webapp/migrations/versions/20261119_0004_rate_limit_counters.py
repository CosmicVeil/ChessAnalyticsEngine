"""Add shared fixed-window counters for IP-based rate limits.

Revision ID: 20261119_0004
Revises: 20261116_0003
Create Date: 2026-11-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20261119_0004"
down_revision: str | None = "20261116_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rate_limit_counters",
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("key", "window_start"),
    )


def downgrade() -> None:
    op.drop_table("rate_limit_counters")

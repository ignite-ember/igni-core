"""when each chat last finished a run, and when the person last looked at it

Revision ID: c8e4f2a6b9d1
Revises: b5d8e1c3a7f9
Create Date: 2026-11-24 00:00:00.000000

A chat whose run finished after the person last looked is unread, and the
sidebar marks it. Global, like the session → folder registry, so what the
person has seen follows them across windows, devices and VMs.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8e4f2a6b9d1"
down_revision: str | Sequence[str] | None = "b5d8e1c3a7f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ember_session_reads",
        sa.Column("org", sa.String(), server_default="", nullable=False),
        sa.Column("producer", sa.String(), server_default="", nullable=False),
        sa.Column("project", sa.String(), server_default="", nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("finished_at", sa.BigInteger(), nullable=True),
        sa.Column("seen_at", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("org", "producer", "session_id", name="pk_ember_session_reads"),
    )


def downgrade() -> None:
    op.drop_table("ember_session_reads")

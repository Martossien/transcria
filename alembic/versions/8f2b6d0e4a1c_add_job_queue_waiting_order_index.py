"""add job queue waiting order index

Revision ID: 8f2b6d0e4a1c
Revises: cc0c0227a415
Create Date: 2026-05-31 19:45:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "8f2b6d0e4a1c"
down_revision = "cc0c0227a415"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_job_queue_waiting_order",
        "job_queue",
        ["status", "base_priority", "aging_bonus", "position", "submitted_at"],
        unique=False,
        postgresql_where=sa.text("status = 'waiting'"),
    )


def downgrade() -> None:
    op.drop_index("ix_job_queue_waiting_order", table_name="job_queue")


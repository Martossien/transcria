"""add job_timing (historique des durées — modèle de temps calibré machine)

Revision ID: d1e8f2a4c6b9
Revises: c9f3d7a1e5b2
Create Date: 2026-07-04 15:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d1e8f2a4c6b9"
down_revision = "c9f3d7a1e5b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_timing",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("profile_id", sa.String(length=40), nullable=False),
        sa.Column("stage", sa.String(length=40), nullable=False),
        sa.Column("audio_seconds", sa.Float(), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_job_timing_profile_stage", "job_timing",
        ["profile_id", "stage", "recorded_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_job_timing_profile_stage", table_name="job_timing")
    op.drop_table("job_timing")

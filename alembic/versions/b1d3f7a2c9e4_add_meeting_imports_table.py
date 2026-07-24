"""meeting_imports : idempotence des imports de réunion (A0, ADR-001 D2)

Relie un artefact de réunion externe à un job TranscrIA sous `UNIQUE(dedup_key)`.
La `dedup_key` est NON-NULLE (SHA-256) : une contrainte UNIQUE laisserait passer
plusieurs NULL, donc un fallback à clé nulle ne protégerait pas. Un webhook rejoué
— ou deux webhooks simultanés — n'obtient qu'un seul job.

Revision ID: b1d3f7a2c9e4
Revises: a9c4e7f2b8d1
Create Date: 2026-07-24 20:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b1d3f7a2c9e4"
down_revision = "a9c4e7f2b8d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "meeting_imports",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("dedup_key", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=True),
        sa.Column("provider_account_id", sa.String(length=255), nullable=True),
        sa.Column("external_occurrence_id", sa.String(length=255), nullable=True),
        sa.Column("external_artifact_id", sa.String(length=255), nullable=True),
        sa.Column("artifact_type", sa.String(length=64), nullable=True),
        sa.Column("artifact_variant", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        # Référence souple (pas de FK) : audit d'import durable, survit à une purge de job.
        sa.Column("job_id", sa.String(length=36), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_meeting_imports_dedup_key", "meeting_imports",
                    ["dedup_key"], unique=True)
    op.create_index("ix_meeting_imports_job_id", "meeting_imports", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_meeting_imports_job_id", table_name="meeting_imports")
    op.drop_index("ix_meeting_imports_dedup_key", table_name="meeting_imports")
    op.drop_table("meeting_imports")

"""meeting_sessions + meeting_runners : l'intention de réunion et ses exécutants (vague 3).

Additive — cf. docs/archive/UI_REUNIONS_WORKFLOW.md §6.1/§6.6. `meeting_ref_encrypted` ne reçoit QUE
des valeurs chiffrées (module meeting_ref_crypto, préfixe enc1:).

Revision ID: c8e5a1f9d2b7
Revises: b1d3f7a2c9e4
"""
import sqlalchemy as sa
from alembic import op

revision = "c8e5a1f9d2b7"
down_revision = "b1d3f7a2c9e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "meeting_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("meeting_ref_encrypted", sa.Text, nullable=False),
        sa.Column("ref_fingerprint", sa.String(64), nullable=False),
        sa.Column("meeting_title", sa.String(255), nullable=False, server_default=""),
        sa.Column("language", sa.String(8), nullable=False, server_default="fr"),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", sa.String(24), nullable=False, server_default="planned"),
        sa.Column("claimed_by", sa.String(64), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_meeting_sessions_ref_fingerprint", "meeting_sessions", ["ref_fingerprint"])
    op.create_index("ix_meeting_sessions_owner_id", "meeting_sessions", ["owner_id"])
    op.create_index("ix_meeting_sessions_job_id", "meeting_sessions", ["job_id"])
    op.create_index("ix_meeting_sessions_state", "meeting_sessions", ["state"])
    op.create_index("ix_meeting_sessions_scheduled_at", "meeting_sessions", ["scheduled_at"])

    op.create_table(
        "meeting_runners",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("capacity", sa.Integer, nullable=False, server_default="1"),
        sa.Column("active_sessions", sa.Integer, nullable=False, server_default="0"),
        sa.Column("platforms_json", sa.Text, nullable=False, server_default="[]"),
        sa.Column("images_json", sa.Text, nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_table("meeting_runners")
    op.drop_index("ix_meeting_sessions_scheduled_at", table_name="meeting_sessions")
    op.drop_index("ix_meeting_sessions_state", table_name="meeting_sessions")
    op.drop_index("ix_meeting_sessions_job_id", table_name="meeting_sessions")
    op.drop_index("ix_meeting_sessions_owner_id", table_name="meeting_sessions")
    op.drop_index("ix_meeting_sessions_ref_fingerprint", table_name="meeting_sessions")
    op.drop_table("meeting_sessions")

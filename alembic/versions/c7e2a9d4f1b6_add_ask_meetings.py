"""« Discuter avec mes réunions » : tables ``ask_requests``, ``ask_turns``, ``ask_turn_citations``
(docs/DISCUTER_REUNIONS.md § 2.1, § 2.7, lot B).

Additive. Les demandes sont écrites par le web et consommées par le worker ; les tours forment la
mémoire courte par utilisateur (rétention 30 jours par défaut) ; les citations relient un tour aux
jobs qu'il cite — ``ON DELETE CASCADE`` vers ``jobs`` et ``ask_turns``.

IDEMPOTENTE (patron ``a7c3e9f1b2d4``).

Revision ID: c7e2a9d4f1b6
Revises: b4d1f6a8c2e9
"""
import sqlalchemy as sa
from alembic import op

revision = "c7e2a9d4f1b6"
down_revision = "b4d1f6a8c2e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if not insp.has_table("ask_requests"):
        op.create_table(
            "ask_requests",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("conversation_id", sa.String(36), nullable=False),
            sa.Column("question", sa.Text, nullable=False),
            sa.Column("locale", sa.String(8), nullable=False, server_default="fr"),
            sa.Column("include_confidential", sa.Boolean, nullable=False, server_default=sa.false()),
            sa.Column("level_requested", sa.Integer, nullable=False, server_default="1"),
            sa.Column("target_job_id", sa.String(36), nullable=True),
            sa.Column("status", sa.String(12), nullable=False, server_default="waiting"),
            sa.Column("answer", sa.Text, nullable=False, server_default=""),
            sa.Column("cited_job_ids_json", sa.Text, nullable=False, server_default="[]"),
            sa.Column("level_reached", sa.Integer, nullable=False, server_default="0"),
            sa.Column("error", sa.Text, nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_ask_requests_user_id", "ask_requests", ["user_id"])
        op.create_index("ix_ask_requests_conversation_id", "ask_requests", ["conversation_id"])
        op.create_index("ix_ask_requests_status", "ask_requests", ["status"])
    if not insp.has_table("ask_turns"):
        op.create_table(
            "ask_turns",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("conversation_id", sa.String(36), nullable=False),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("role", sa.String(12), nullable=False),
            sa.Column("text", sa.Text, nullable=False, server_default=""),
            sa.Column("level", sa.Integer, nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_ask_turns_conversation_id", "ask_turns", ["conversation_id"])
        op.create_index("ix_ask_turns_user_id", "ask_turns", ["user_id"])
        op.create_index("ix_ask_turns_created_at", "ask_turns", ["created_at"])
    if not insp.has_table("ask_turn_citations"):
        op.create_table(
            "ask_turn_citations",
            sa.Column("turn_id", sa.String(36), sa.ForeignKey("ask_turns.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True),
        )
        op.create_index("ix_ask_turn_citations_job_id", "ask_turn_citations", ["job_id"])


def downgrade() -> None:
    op.drop_table("ask_turn_citations")
    op.drop_table("ask_turns")
    op.drop_table("ask_requests")

"""Gouvernance LLM cloud : validations admin + interdiction par groupe (docs/LLM_CLOUD.md § 3.2).

Additive : table ``llm_cloud_acknowledgements`` (append-only, caducité par ``text_version``) et
colonne ``groups.llm_cloud_forbidden`` (défaut faux : aucune installation ne change de comportement).

Revision ID: a7c3e9f1b2d4
Revises: e5a9c3f7b1d8
"""
import sqlalchemy as sa
from alembic import op

revision = "a7c3e9f1b2d4"
down_revision = "e5a9c3f7b1d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_cloud_acknowledgements",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("text_version", sa.String(80), nullable=False),
        sa.Column("acknowledged_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("acknowledged_by_username", sa.String(80), nullable=False, server_default=""),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("connectors_allowed", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("usages_json", sa.Text, nullable=False, server_default='{"pipeline": true}'),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_llm_cloud_acknowledgements_provider", "llm_cloud_acknowledgements", ["provider"])
    with op.batch_alter_table("groups") as batch:
        batch.add_column(sa.Column("llm_cloud_forbidden", sa.Boolean, nullable=False,
                                   server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("groups") as batch:
        batch.drop_column("llm_cloud_forbidden")
    op.drop_index("ix_llm_cloud_acknowledgements_provider", table_name="llm_cloud_acknowledgements")
    op.drop_table("llm_cloud_acknowledgements")

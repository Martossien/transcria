"""add meeting_type_templates (types de réunion personnalisés)

Revision ID: c9f3d7a1e5b2
Revises: b7c4e1a9f3d2
Create Date: 2026-07-03 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "c9f3d7a1e5b2"
down_revision = "b7c4e1a9f3d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "meeting_type_templates",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("definition_json", sa.Text(), nullable=False),
        sa.Column("logo_blob", sa.LargeBinary(), nullable=True),
        sa.Column("logo_mime", sa.String(length=40), nullable=False),
        sa.Column("scope", sa.String(length=10), nullable=False),
        sa.Column("group_id", sa.String(length=36), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_meeting_type_templates_slug", "meeting_type_templates", ["slug"])
    op.create_index("ix_meeting_type_templates_name", "meeting_type_templates", ["name"])
    op.create_index("ix_meeting_type_templates_scope", "meeting_type_templates", ["scope"])
    op.create_index("ix_meeting_type_templates_group_id", "meeting_type_templates", ["group_id"])
    op.create_index("ix_meeting_type_templates_created_by", "meeting_type_templates", ["created_by"])


def downgrade() -> None:
    op.drop_index("ix_meeting_type_templates_created_by", table_name="meeting_type_templates")
    op.drop_index("ix_meeting_type_templates_group_id", table_name="meeting_type_templates")
    op.drop_index("ix_meeting_type_templates_scope", table_name="meeting_type_templates")
    op.drop_index("ix_meeting_type_templates_name", table_name="meeting_type_templates")
    op.drop_index("ix_meeting_type_templates_slug", table_name="meeting_type_templates")
    op.drop_table("meeting_type_templates")

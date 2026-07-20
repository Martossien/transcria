"""api_tokens : jetons d'API personnels (chantier identité lot 4)

Format servi une seule fois `tia_<token_id>_<secret>` ; seul le SHA-256 du
secret est stocké. token_id est la partie PUBLIQUE (lookup O(1), unique).
Révocation soft (revoked_at) : la trace d'audit survit à la révocation.
last_used_at est mis à jour au plus 1×/min (le polling /status n'écrit pas
à chaque hit).

Revision ID: a9c4e7f2b8d1
Revises: f6b2d8e1a3c7
Create Date: 2026-07-20 18:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a9c4e7f2b8d1"
down_revision = "f6b2d8e1a3c7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_id", sa.String(length=16), nullable=False),
        sa.Column("secret_hash", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_api_tokens_user_id", "api_tokens", ["user_id"])
    op.create_index("ix_api_tokens_token_id", "api_tokens", ["token_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_api_tokens_token_id", table_name="api_tokens")
    op.drop_index("ix_api_tokens_user_id", table_name="api_tokens")
    op.drop_table("api_tokens")

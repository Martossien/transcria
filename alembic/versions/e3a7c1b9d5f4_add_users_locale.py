"""add users.locale (préférence de langue de l'interface)

Revision ID: e3a7c1b9d5f4
Revises: d1e8f2a4c6b9
Create Date: 2026-07-07 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "e3a7c1b9d5f4"
down_revision = "d1e8f2a4c6b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Colonne nullable (NULL = suivre le navigateur / le défaut d'instance) → aucune valeur
    # par défaut à propager, compatible SQLite et PostgreSQL.
    op.add_column("users", sa.Column("locale", sa.String(length=8), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "locale")

"""meeting_runners.token_id : révocation PRÉCISE par exécutant (v1 de la page admin).

Le heartbeat enregistre l'identifiant PUBLIC du jeton utilisé (jamais le secret) —
le bouton « Révoquer » invalide CE jeton, pas ceux des autres exécutants.

Revision ID: d4f7b2e8c1a5
Revises: c8e5a1f9d2b7
"""
import sqlalchemy as sa
from alembic import op

revision = "d4f7b2e8c1a5"
down_revision = "c8e5a1f9d2b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("meeting_runners",
                  sa.Column("token_id", sa.String(32), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("meeting_runners", "token_id")

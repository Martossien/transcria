"""Fiches de réunion : temps de parole (``job_cards.speaking_json``, ``unnamed_speech_pct``) —
« qui parle le plus de… », point relevé par les utilisateurs (2026-11).

Additive, IDEMPOTENTE (colonnes ajoutées seulement si absentes). Les fiches existantes sont
recalculées au démarrage du worker (la version de projection passe à 2).

Revision ID: e9a4c6d2b8f1
Revises: d8f3b5c1e7a2
"""
import sqlalchemy as sa
from alembic import op

revision = "e9a4c6d2b8f1"
down_revision = "d8f3b5c1e7a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("job_cards")}
    with op.batch_alter_table("job_cards") as batch:
        if "speaking_json" not in columns:
            batch.add_column(sa.Column("speaking_json", sa.Text, nullable=False, server_default="[]"))
        if "unnamed_speech_pct" not in columns:
            batch.add_column(sa.Column("unnamed_speech_pct", sa.Integer, nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("job_cards") as batch:
        batch.drop_column("unnamed_speech_pct")
        batch.drop_column("speaking_json")

"""« Discuter avec mes réunions » : colonne ``ask_requests.stage`` — l'étape en cours écrite par le
worker (« pourquoi j'attends » : verrou LLM, chargement du modèle, rédaction, synthèses…).

Additive, IDEMPOTENTE (la colonne n'est ajoutée que si elle manque — patron ``a7c3e9f1b2d4``).

Revision ID: d8f3b5c1e7a2
Revises: c7e2a9d4f1b6
"""
import sqlalchemy as sa
from alembic import op

revision = "d8f3b5c1e7a2"
down_revision = "c7e2a9d4f1b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("ask_requests")}
    if "stage" not in columns:
        with op.batch_alter_table("ask_requests") as batch:
            batch.add_column(sa.Column("stage", sa.String(24), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("ask_requests") as batch:
        batch.drop_column("stage")

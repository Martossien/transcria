"""Fiches de réunion : table ``job_cards`` (docs/DISCUTER_REUNIONS.md § 2.3, lot A).

Additive : une fiche par job, projection courte de ``context/meeting_context.json`` et
``context/participants.json``. Donnée DÉRIVÉE — la table naît vide, le process qui exécute la
file la remplit au démarrage (``job_cards.hooks.start_card_catchup``) et
``python -m transcria.maintenance.cli rebuild-job-cards`` la reconstruit à la demande.

``ON DELETE CASCADE`` : supprimer un job supprime sa fiche sur tous les chemins (PostgreSQL).

IDEMPOTENTE (patron ``a7c3e9f1b2d4``) : ``db.create_all()`` a pu poser la table avant
``alembic upgrade head`` si le nouveau code a démarré avant la migration.

Revision ID: b4d1f6a8c2e9
Revises: a7c3e9f1b2d4
"""
import sqlalchemy as sa
from alembic import op

revision = "b4d1f6a8c2e9"
down_revision = "a7c3e9f1b2d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("job_cards"):
        return
    op.create_table(
        "job_cards",
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("title", sa.String(255), nullable=False, server_default=""),
        sa.Column("meeting_type", sa.String(255), nullable=False, server_default=""),
        sa.Column("meeting_date", sa.Date, nullable=True),
        sa.Column("date_is_import_fallback", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("confidential", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("sensitivity", sa.String(20), nullable=False, server_default="normal"),
        sa.Column("language", sa.String(8), nullable=False, server_default=""),
        sa.Column("participants_json", sa.Text, nullable=False, server_default="[]"),
        sa.Column("topic", sa.Text, nullable=False, server_default=""),
        sa.Column("objective", sa.Text, nullable=False, server_default=""),
        sa.Column("agenda", sa.Text, nullable=False, server_default=""),
        sa.Column("abstract", sa.Text, nullable=False, server_default=""),
        sa.Column("decisions_json", sa.Text, nullable=False, server_default="[]"),
        sa.Column("actions_json", sa.Text, nullable=False, server_default="[]"),
        sa.Column("next_date", sa.String(120), nullable=False, server_default=""),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("job_cards")

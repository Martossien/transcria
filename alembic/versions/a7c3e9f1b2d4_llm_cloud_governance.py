"""Gouvernance LLM cloud : validations admin + interdiction par groupe (docs/LLM_CLOUD.md § 3.2).

Additive : table ``llm_cloud_acknowledgements`` (append-only, caducité par ``text_version``) et
colonne ``groups.llm_cloud_forbidden`` (défaut faux : aucune installation ne change de comportement).

IDEMPOTENTE : chaque objet n'est créé que s'il manque. Cas réel (2026-11-01) : l'application a
démarré avec le nouveau code avant que la migration ne passe — ``db.create_all()`` avait posé la
table (jamais une colonne sur une table existante), et ``alembic upgrade head`` plantait ensuite
sur « la relation existe déjà » ⇒ service bloqué au démarrage. On rattrape l'état au lieu d'exiger
une base intacte.

Revision ID: a7c3e9f1b2d4
Revises: e5a9c3f7b1d8
"""
import sqlalchemy as sa
from alembic import op

revision = "a7c3e9f1b2d4"
down_revision = "e5a9c3f7b1d8"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def upgrade() -> None:
    insp = _inspector()
    if not insp.has_table("llm_cloud_acknowledgements"):
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
    existing_indexes = {ix["name"] for ix in _inspector().get_indexes("llm_cloud_acknowledgements")}
    if "ix_llm_cloud_acknowledgements_provider" not in existing_indexes:
        op.create_index("ix_llm_cloud_acknowledgements_provider", "llm_cloud_acknowledgements", ["provider"])
    group_columns = {col["name"] for col in _inspector().get_columns("groups")}
    if "llm_cloud_forbidden" not in group_columns:
        with op.batch_alter_table("groups") as batch:
            batch.add_column(sa.Column("llm_cloud_forbidden", sa.Boolean, nullable=False,
                                       server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("groups") as batch:
        batch.drop_column("llm_cloud_forbidden")
    op.drop_index("ix_llm_cloud_acknowledgements_provider", table_name="llm_cloud_acknowledgements")
    op.drop_table("llm_cloud_acknowledgements")

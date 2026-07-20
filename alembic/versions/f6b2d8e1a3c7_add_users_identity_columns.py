"""users : colonnes d'identité fédérée (chantier identité lot 0)

identity_source : provenance du compte ("local" historique ; "oidc"/"ldap"/"proxy"
aux lots suivants) — les chemins mot-de-passe refusent si != local.
external_subject : identifiant STABLE chez le fournisseur (sub OIDC, objectGUID AD) ;
le rapprochement JIT se fait sur (identity_source, external_subject), jamais l'email.
last_identity_sync : dernière resynchronisation des attributs au login fédéré.

Revision ID: f6b2d8e1a3c7
Revises: e3a7c1b9d5f4
Create Date: 2026-07-20 10:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "f6b2d8e1a3c7"
down_revision = "e3a7c1b9d5f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default "local" : les lignes EXISTANTES deviennent explicitement locales
    # (aucun NULL ambigu) ; compatible SQLite et PostgreSQL.
    op.add_column("users", sa.Column("identity_source", sa.String(length=16),
                                     nullable=False, server_default="local"))
    op.add_column("users", sa.Column("external_subject", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("last_identity_sync", sa.DateTime(timezone=True), nullable=True))
    # Unicité du rapprochement JIT : un sujet externe = un compte, par source.
    op.create_index("ix_users_identity_external", "users",
                    ["identity_source", "external_subject"], unique=True,
                    postgresql_where=sa.text("external_subject IS NOT NULL"),
                    sqlite_where=sa.text("external_subject IS NOT NULL"))


def downgrade() -> None:
    op.drop_index("ix_users_identity_external", table_name="users")
    op.drop_column("users", "last_identity_sync")
    op.drop_column("users", "external_subject")
    op.drop_column("users", "identity_source")

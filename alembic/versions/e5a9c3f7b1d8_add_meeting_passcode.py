"""meeting_sessions.meeting_passcode_encrypted : franchir une salle PROTÉGÉE.

Trou trouvé à la revue de complétude Jitsi (2026-07-30) : le bot DÉTECTAIT une salle
verrouillée (`password_required`) mais rien ne permettait de lui fournir le code — une
réunion protégée par mot de passe (un clic dans Jitsi, courant en entreprise) était
inaccessible sans recours. Le code est un SECRET : même traitement que `meeting_ref`
(chiffré au repos, déchiffré au seul claim du runner, jamais journalisé ni affiché).

Revision ID: e5a9c3f7b1d8
Revises: d4f7b2e8c1a5
"""
import sqlalchemy as sa
from alembic import op

revision = "e5a9c3f7b1d8"
down_revision = "d4f7b2e8c1a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NULL = salle sans code (cas courant) — pas de server_default : « aucun code » et
    # « code vide » doivent rester distincts.
    op.add_column("meeting_sessions",
                  sa.Column("meeting_passcode_encrypted", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("meeting_sessions", "meeting_passcode_encrypted")

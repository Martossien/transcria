"""Modèles `MeetingSession` (l'INTENTION de réunion) et `MeetingRunner` (vague 3, D2).

L'intention répond à « que doit-il se passer, quand, pour qui ? » ; `MeetingImport` répond à
« cet artefact a-t-il déjà créé un job ? » (idempotence par artefact). Ne pas fusionner : une
session peut échouer sans jamais produire d'artefact, un artefact peut arriver par un
connecteur officiel sans session (docs/archive/UI_REUNIONS_WORKFLOW.md §4 D2).

`meeting_ref` est CHIFFRÉ au repos (module unique `meeting_ref_crypto`, à ratifier en revue
sécurité) — l'affichage passe par `meeting_title`, jamais par la référence. Les transitions
d'état passent par `session_states` (machine PURE) : le modèle stocke, il ne décide pas.
"""
import uuid
from datetime import datetime, timezone

from transcria.database import db
from transcria.ingestion.session_states import PLANNED


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MeetingSession(db.Model):
    __tablename__ = "meeting_sessions"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    job_id = db.Column(db.String(36), db.ForeignKey("jobs.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    provider = db.Column(db.String(32), nullable=False)
    meeting_ref_encrypted = db.Column(db.Text, nullable=False)     # enc1:… uniquement
    # Empreinte NON réversible (sha256) de la référence normalisée — sert UNIQUEMENT à
    # détecter « cette réunion est déjà planifiée » sans jamais déchiffrer ni stocker en clair.
    ref_fingerprint = db.Column(db.String(64), nullable=False, index=True)
    # Code d'accès de la salle (« mot de passe » Jitsi / passcode), CHIFFRÉ comme la
    # référence et soumis aux mêmes règles : jamais affiché, jamais journalisé, déchiffré
    # au SEUL claim du runner. NULL = salle sans code (cas courant).
    meeting_passcode_encrypted = db.Column(db.Text, nullable=True)
    meeting_title = db.Column(db.String(255), nullable=False, default="", server_default="")
    language = db.Column(db.String(8), nullable=False, default="fr", server_default="fr")
    scheduled_at = db.Column(db.DateTime(timezone=True), nullable=True, index=True)  # NULL = dès que possible
    state = db.Column(db.String(24), nullable=False, default=PLANNED, server_default=PLANNED, index=True)
    claimed_by = db.Column(db.String(64), nullable=True)
    claimed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    ended_at = db.Column(db.DateTime(timezone=True), nullable=True)
    attempt_count = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    # Catégorie + message COURT, sans secret ni référence de réunion (règle du module crypto).
    last_error = db.Column(db.Text, nullable=True)
    next_retry_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow,
                           onupdate=_utcnow)

    def to_public_dict(self) -> dict:
        """Vue API humaine — SANS la référence de réunion (chiffrée ou non)."""
        return {
            "id": self.id,
            "job_id": self.job_id,
            "provider": self.provider,
            "meeting_title": self.meeting_title,
            "language": self.language,
            "scheduled_at": self.scheduled_at.isoformat() if self.scheduled_at else None,
            "state": self.state,
            "attempt_count": self.attempt_count,
            "last_error": self.last_error,
            "next_retry_at": self.next_retry_at.isoformat() if self.next_retry_at else None,
        }


class MeetingRunner(db.Model):
    """Un exécutant annoncé par heartbeat — alimente `availability` (la carte « Réunion »
    n'apparaît que si quelqu'un peut réellement lancer un bot) et la page admin."""

    __tablename__ = "meeting_runners"

    name = db.Column(db.String(64), primary_key=True)
    last_seen = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    capacity = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    active_sessions = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    platforms_json = db.Column(db.Text, nullable=False, default="[]", server_default="[]")   # ids du catalogue
    images_json = db.Column(db.Text, nullable=False, default="[]", server_default="[]")
    # Identifiant PUBLIC du jeton utilisé par cet exécutant (jamais le secret) — permet la
    # révocation PRÉCISE depuis la page admin (invalider CE runner, pas les autres).
    token_id = db.Column(db.String(32), nullable=False, default="", server_default="")      # [{name, digest}]

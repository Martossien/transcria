"""Tables ``ask_requests``, ``ask_turns``, ``ask_turn_citations`` (docs/DISCUTER_REUNIONS.md § 2.1, § 2.7).

Une demande est écrite par le web (202) et consommée par le worker ; la conversation est une
mémoire COURTE par utilisateur (rétention ``workflow.ask_meetings.retention_days``, 30 jours).
Les citations d'un tour sont des lignes avec clé étrangère vers ``jobs`` : supprimer une réunion
supprime les tours qui la citent (événement ORM ci-dessous, et ``ON DELETE CASCADE`` en base pour
les lignes de citation elles-mêmes).
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import event

from transcria.database import db
from transcria.jobs.models import Job

STATUS_WAITING = "waiting"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

LEVEL_CARDS = 1
LEVEL_SUMMARIES = 2
LEVEL_TRANSCRIPT = 3

#: Étapes d'une demande en cours — ce que la page affiche à la place d'un « en attente » unique.
STAGE_QUEUED = "queued"            # une autre demande est traitée avant celle-ci
STAGE_LLM_BUSY = "llm_busy"        # le verrou LLM est tenu par un traitement
STAGE_VRAM_WAIT = "vram_wait"      # pas assez de VRAM pour charger la LLM, nouvelle tentative plus tard
STAGE_LLM_LOADING = "llm_loading"  # chargement du modèle (modes locaux seulement)
STAGE_GENERATING = "generating"    # appel en cours, niveau fiches ou transcription
STAGE_SUMMARIES = "summaries"      # les fiches n'ont pas suffi : lecture des synthèses


def _now() -> datetime:
    return datetime.now(UTC)


def _dump_ids(ids: list[str] | None) -> str:
    return json.dumps([str(i) for i in (ids or [])], ensure_ascii=False)


def _load_ids(raw: str | None) -> list[str]:
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        return []
    return [str(i) for i in data] if isinstance(data, list) else []


class AskRequest(db.Model):
    __tablename__ = "ask_requests"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.String(36), db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    conversation_id = db.Column(db.String(36), nullable=False, index=True)
    question = db.Column(db.Text, nullable=False)
    #: Langue de la RÉPONSE = locale de la requête web (plusieurs réunions, plusieurs propriétaires).
    locale = db.Column(db.String(8), nullable=False, default="fr", server_default="fr")
    include_confidential = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    level_requested = db.Column(db.Integer, nullable=False, default=LEVEL_CARDS, server_default="1")
    #: Niveau 3 : la réunion dont on lit la transcription (une seule). Pas de clé étrangère : un
    #: identifiant inexistant doit être traité comme une réunion INVISIBLE par le worker (même
    #: message), jamais révélé par une erreur d'intégrité à l'écriture.
    target_job_id = db.Column(db.String(36), nullable=True)
    status = db.Column(db.String(12), nullable=False, default=STATUS_WAITING, server_default=STATUS_WAITING, index=True)
    answer = db.Column(db.Text, nullable=False, default="", server_default="")
    cited_job_ids_json = db.Column(db.Text, nullable=False, default="[]", server_default="[]")
    level_reached = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    #: Étape en cours, écrite par le worker (« pourquoi j'attends ») : voir STAGE_* ; vide = rien de
    #: plus précis que le statut. Un libellé par langue dans ``messages`` (clé ``stage_<étape>``).
    stage = db.Column(db.String(24), nullable=False, default="", server_default="")
    error = db.Column(db.Text, nullable=False, default="", server_default="")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)
    started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    done_at = db.Column(db.DateTime(timezone=True), nullable=True)

    @property
    def cited_job_ids(self) -> list[str]:
        return _load_ids(self.cited_job_ids_json)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "question": self.question,
            "locale": self.locale,
            "include_confidential": bool(self.include_confidential),
            "level_requested": int(self.level_requested),
            "target_job_id": self.target_job_id,
            "status": self.status,
            "answer": self.answer,
            "cited_job_ids": self.cited_job_ids,
            "level_reached": int(self.level_reached),
            "stage": self.stage,
            "error": self.error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "done_at": self.done_at.isoformat() if self.done_at else None,
        }


class AskTurn(db.Model):
    __tablename__ = "ask_turns"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    conversation_id = db.Column(db.String(36), nullable=False, index=True)
    user_id = db.Column(db.String(36), db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    role = db.Column(db.String(12), nullable=False)          # user | assistant
    text = db.Column(db.Text, nullable=False, default="", server_default="")
    level = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now, index=True)

    citations = db.relationship("AskTurnCitation", back_populates="turn", cascade="all, delete-orphan",
                                passive_deletes=True)

    @property
    def cited_job_ids(self) -> list[str]:
        return [c.job_id for c in list(self.citations)]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "role": self.role,
            "text": self.text,
            "level": int(self.level),
            "cited_job_ids": self.cited_job_ids,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class AskTurnCitation(db.Model):
    """Un tour cite un job. Clé étrangère vers ``jobs`` : la suppression de la réunion retire la
    ligne en base (PostgreSQL) ; l'événement ORM ci-dessous retire le TOUR entier."""

    __tablename__ = "ask_turn_citations"

    turn_id = db.Column(db.String(36), db.ForeignKey("ask_turns.id", ondelete="CASCADE"), primary_key=True)
    job_id = db.Column(db.String(36), db.ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True, index=True)

    turn = db.relationship(AskTurn, back_populates="citations")


@event.listens_for(Job, "before_delete")
def _forget_turns_citing_job(mapper, connection, job) -> None:
    """Supprimer une réunion supprime les tours qui la citent (§ 2.7) — sur SQLite aussi, qui
    n'applique pas les clés étrangères. Chemins couverts : ``JobStore.delete_job`` et la purge
    de rétention (tous deux par l'ORM)."""
    turn_ids = list(connection.execute(
        db.select(AskTurnCitation.turn_id).where(AskTurnCitation.job_id == job.id)).scalars())
    if not turn_ids:
        connection.execute(db.delete(AskRequest).where(AskRequest.cited_job_ids_json.like(f'%"{job.id}"%')))
        return
    connection.execute(db.delete(AskTurnCitation).where(AskTurnCitation.turn_id.in_(turn_ids)))
    connection.execute(db.delete(AskTurn).where(AskTurn.id.in_(turn_ids)))
    # La réponse d'une demande cite la réunion aussi (texte + identifiants) : même sort.
    connection.execute(db.delete(AskRequest).where(AskRequest.cited_job_ids_json.like(f'%"{job.id}"%')))

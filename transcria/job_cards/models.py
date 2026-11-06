"""Table ``job_cards`` — une fiche par job, projection courte de son contexte.

La fiche est une DONNÉE DÉRIVÉE : tout se recalcule depuis ``context/meeting_context.json`` et
``context/participants.json`` (``JobCardStore.refresh``, rattrapage ``rebuild-job-cards``). Elle
ne porte ni le propriétaire ni le titre du job : le lecteur part toujours de
``JobStore.list_for_user`` et a donc la ligne ``jobs`` sous la main — recopier ces colonnes
créerait une seconde vérité à tenir à jour.

Suppression : la clé étrangère est ``ON DELETE CASCADE`` (PostgreSQL) ET la relation porte une
cascade ORM — SQLite n'applique pas les clés étrangères (aucun ``PRAGMA foreign_keys`` dans
l'application, vérifié au lot 0), et ``JobStore.delete_job`` supprime par l'ORM.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

from transcria.database import db
from transcria.jobs.models import Job


class JobCard(db.Model):
    __tablename__ = "job_cards"

    job_id = db.Column(db.String(36), db.ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    # Titre du CONTEXTE (étape 4, sinon titre suggéré) ; vide ⇒ le lecteur affiche le titre du job.
    title = db.Column(db.String(255), nullable=False, default="", server_default="")
    # Type choisi à l'étape 4, sinon type suggéré par la LLM.
    meeting_type = db.Column(db.String(255), nullable=False, default="", server_default="")
    meeting_date = db.Column(db.Date, nullable=True)
    # Vrai : la date du contexte était vide ou illisible, ``meeting_date`` est la date de création du job.
    date_is_import_fallback = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    confidential = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    sensitivity = db.Column(db.String(20), nullable=False, default="normal", server_default="normal")
    language = db.Column(db.String(8), nullable=False, default="", server_default="")
    participants_json = db.Column(db.Text, nullable=False, default="[]", server_default="[]")
    # Sujet, objectif, ordre du jour : étape 4 si enregistrée, sinon suggestions de la LLM.
    topic = db.Column(db.Text, nullable=False, default="", server_default="")
    objective = db.Column(db.Text, nullable=False, default="", server_default="")
    agenda = db.Column(db.Text, nullable=False, default="", server_default="")
    abstract = db.Column(db.Text, nullable=False, default="", server_default="")
    decisions_json = db.Column(db.Text, nullable=False, default="[]", server_default="[]")
    actions_json = db.Column(db.Text, nullable=False, default="[]", server_default="[]")
    next_date = db.Column(db.String(120), nullable=False, default="", server_default="")
    # Empreinte de tout ce dont la fiche dépend (projection.source_fingerprint) : identique ⇒ rien à écrire.
    source_sha256 = db.Column(db.String(64), nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    job = db.relationship(
        Job, backref=db.backref("card", uselist=False, cascade="all, delete-orphan"))

    @staticmethod
    def _load_list(raw: str | None) -> list:
        try:
            data = json.loads(raw or "[]")
        except ValueError:
            return []
        return data if isinstance(data, list) else []

    @property
    def participants(self) -> list[dict]:
        return [p for p in self._load_list(self.participants_json) if isinstance(p, dict)]

    @property
    def decisions(self) -> list[str]:
        return [str(d) for d in self._load_list(self.decisions_json)]

    @property
    def actions(self) -> list[str]:
        return [str(a) for a in self._load_list(self.actions_json)]

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "title": self.title,
            "meeting_type": self.meeting_type,
            "meeting_date": self.meeting_date.isoformat() if self.meeting_date else None,
            "date_is_import_fallback": bool(self.date_is_import_fallback),
            "confidential": bool(self.confidential),
            "sensitivity": self.sensitivity,
            "language": self.language,
            "participants": self.participants,
            "topic": self.topic,
            "objective": self.objective,
            "agenda": self.agenda,
            "abstract": self.abstract,
            "decisions": self.decisions,
            "actions": self.actions,
            "next_date": self.next_date,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

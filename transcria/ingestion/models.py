"""Modèle `MeetingImport` — mapping artefact de réunion externe → job (A0, ADR-001 D2).

Idempotence côté serveur : une `dedup_key` NON-NULLE (SHA-256, jamais NULL — une
contrainte `UNIQUE` PostgreSQL laisserait passer plusieurs NULL) sous contrainte
`UNIQUE`. Le connecteur calcule sa clé composite (provider + compte + occurrence +
artefact) et la transmet ; TranscrIA la hache et déduplique dessus, sans connaître la
structure de clé propre à chaque plateforme.
"""
import uuid
from datetime import datetime, timezone

from transcria.database import db


class ImportStatus:
    """États d'un `MeetingImport` (ADR-001 D2). Chaînes stockées telles quelles."""

    RECEIVED = "received"          # créé, pas encore de job
    JOB_CREATED = "job_created"    # job TranscrIA créé et rattaché
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_FINAL = "failed_final"
    IGNORED = "ignored"

    ALL = (RECEIVED, JOB_CREATED, FAILED_RETRYABLE, FAILED_FINAL, IGNORED)


class MeetingImport(db.Model):
    """Un artefact de réunion externe importé (ou en cours d'import) → un job.

    La `dedup_key` porte l'unicité (jamais deux imports pour le même artefact). Les
    champs `provider`/`external_*` sont de l'audit lisible ; ils ne portent PAS
    l'unicité (c'est `dedup_key` qui l'assure, à l'épreuve des NULL).
    """

    __tablename__ = "meeting_imports"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # Clé de déduplication NON-NULLE (SHA-256 hex = 64 chars) — l'unicité vit ici.
    dedup_key = db.Column(db.String(64), unique=True, nullable=False, index=True)
    # Métadonnées d'audit (lisibles) — jamais porteuses de l'unicité.
    provider = db.Column(db.String(64), nullable=True)
    provider_account_id = db.Column(db.String(255), nullable=True)
    external_occurrence_id = db.Column(db.String(255), nullable=True)
    external_artifact_id = db.Column(db.String(255), nullable=True)
    artifact_type = db.Column(db.String(64), nullable=True)
    artifact_variant = db.Column(db.String(64), nullable=True)
    status = db.Column(db.String(32), nullable=False, default=ImportStatus.RECEIVED)
    # Référence SOUPLE (pas de FK) : l'enregistrement d'import est un audit DURABLE — il
    # doit survivre à une purge du job (sinon un ré-import du même artefact repasserait).
    job_id = db.Column(db.String(36), nullable=True, index=True)
    correlation_id = db.Column(db.String(64), nullable=True)
    attempt_count = db.Column(db.Integer, nullable=False, default=0)
    last_error = db.Column(db.Text, nullable=True)
    next_retry_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False,
                           default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False,
                           default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "provider": self.provider,
            "external_occurrence_id": self.external_occurrence_id,
            "external_artifact_id": self.external_artifact_id,
            "status": self.status,
            "job_id": self.job_id,
            "correlation_id": self.correlation_id,
            "attempt_count": self.attempt_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

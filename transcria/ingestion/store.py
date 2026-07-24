"""Store de `MeetingImport` — get-or-create idempotent (A0, ADR-001 D2).

Idempotence à l'épreuve des courses via le pattern maison portable Postgres+SQLite :
INSERT optimiste → sur `IntegrityError` (violation de `UNIQUE(dedup_key)`) → rollback →
relecture de l'existant (cf. `QueueStore.enqueue`). Aucun `on_conflict` dialecte-spécifique.
"""
import hashlib
import logging

from sqlalchemy.exc import IntegrityError

from transcria.database import db
from transcria.ingestion.models import ImportStatus, MeetingImport

logger = logging.getLogger(__name__)


def compute_dedup_key(idempotency_key: str) -> str:
    """`dedup_key` NON-NULLE = SHA-256 hex de la clé d'idempotence fournie par le
    connecteur (qui, lui, l'a composée de provider+compte+occurrence+artefact). On ne
    stocke pas la clé brute ; on déduplique sur son empreinte de longueur fixe (64)."""
    return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()


class MeetingImportStore:
    @staticmethod
    def get_by_dedup_key(dedup_key: str) -> MeetingImport | None:
        return db.session.scalar(
            db.select(MeetingImport).filter_by(dedup_key=dedup_key)
        )

    @staticmethod
    def get_or_create(dedup_key: str, **meta) -> tuple[MeetingImport, bool]:
        """Retourne `(import, created)`. `created=True` ⇒ CET appel a gagné l'INSERT
        (et est donc responsable de créer le job) ; `created=False` ⇒ un import existait
        déjà (doublon / rejeu / course perdue) — l'appelant ne doit PAS créer un 2e job.

        `meta` : `provider`, `provider_account_id`, `external_occurrence_id`,
        `external_artifact_id`, `artifact_type`, `artifact_variant`, `correlation_id`.
        """
        existing = MeetingImportStore.get_by_dedup_key(dedup_key)
        if existing is not None:
            return existing, False
        record = MeetingImport(dedup_key=dedup_key, status=ImportStatus.RECEIVED, **meta)
        db.session.add(record)
        try:
            db.session.commit()
            return record, True
        except IntegrityError:
            # Course : un autre appel a inséré la même dedup_key entre le SELECT et notre
            # COMMIT → on relit l'existant (garanti présent grâce à UNIQUE(dedup_key)).
            db.session.rollback()
            other = MeetingImportStore.get_by_dedup_key(dedup_key)
            if other is None:
                raise  # IntegrityError non liée à l'unicité dedup_key → remonter
            return other, False

    @staticmethod
    def attach_job(dedup_key: str, job_id: str) -> None:
        """Rattache le job créé à l'import (status → job_created)."""
        record = MeetingImportStore.get_by_dedup_key(dedup_key)
        if record is not None:
            record.job_id = job_id
            record.status = ImportStatus.JOB_CREATED
            db.session.commit()

    @staticmethod
    def release(dedup_key: str) -> None:
        """Libère un import provisoire (échec de création de job AVANT rattachement) :
        on le supprime pour qu'un rejeu reparte proprement. Les orphelins par crash
        (import sans job jamais libéré) relèvent de la réconciliation (ADR-001 D2-bis)."""
        record = MeetingImportStore.get_by_dedup_key(dedup_key)
        if record is not None and record.job_id is None:
            db.session.delete(record)
            db.session.commit()

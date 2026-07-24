"""A0 lot 1 — MeetingImport + store idempotent (ADR-001 D2)."""
from __future__ import annotations

import uuid

from transcria.ingestion.models import ImportStatus
from transcria.ingestion.store import MeetingImportStore, compute_dedup_key


def _key() -> str:
    return compute_dedup_key("visio|acct|occ|" + uuid.uuid4().hex)


def test_compute_dedup_key_deterministe_non_null_64():
    k = compute_dedup_key("zoom|a|o|art")
    assert len(k) == 64 and k
    assert k == compute_dedup_key("zoom|a|o|art")      # déterministe
    assert k != compute_dedup_key("zoom|a|o|autre")    # discrimine


class TestStore:
    def test_get_or_create_cree_puis_deduplique(self, app):
        with app.app_context():
            dk = _key()
            rec, created = MeetingImportStore.get_or_create(dk, provider="visio",
                                                            external_occurrence_id="occ-1")
            assert created is True
            assert rec.provider == "visio" and rec.status == ImportStatus.RECEIVED
            # 2e appel même clé : PAS de nouvel enregistrement.
            rec2, created2 = MeetingImportStore.get_or_create(dk, provider="visio")
            assert created2 is False and rec2.id == rec.id

    def test_attach_job_passe_en_job_created(self, app):
        with app.app_context():
            dk = _key()
            MeetingImportStore.get_or_create(dk, provider="zoom")
            MeetingImportStore.attach_job(dk, "job-abc")
            rec = MeetingImportStore.get_by_dedup_key(dk)
            assert rec.job_id == "job-abc" and rec.status == ImportStatus.JOB_CREATED

    def test_release_supprime_import_sans_job(self, app):
        with app.app_context():
            dk = _key()
            MeetingImportStore.get_or_create(dk)
            MeetingImportStore.release(dk)
            assert MeetingImportStore.get_by_dedup_key(dk) is None

    def test_release_conserve_import_deja_rattache(self, app):
        with app.app_context():
            dk = _key()
            MeetingImportStore.get_or_create(dk)
            MeetingImportStore.attach_job(dk, "job-xyz")
            MeetingImportStore.release(dk)  # a un job → ne supprime pas
            rec = MeetingImportStore.get_by_dedup_key(dk)
            assert rec is not None and rec.job_id == "job-xyz"

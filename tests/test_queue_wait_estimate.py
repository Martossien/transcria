"""ETA live (temps restant) + temps d'attente cumulé de la file (modèle de temps)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.fixture
def _clean(app):
    with app.app_context():
        from transcria.database import db
        from transcria.jobs.timing_store import JobTiming
        db.session.query(JobTiming).delete()
        db.session.commit()
    yield


class TestEstimateRemaining:
    def test_decroit_avec_la_progression(self, app, _clean):
        with app.app_context():
            from transcria.workflow.profiles import get_profile
            from transcria.workflow.timing_service import estimate_remaining
            prof = get_profile("dossier_qualite")
            at0 = estimate_remaining(prof, 600, 0)
            at50 = estimate_remaining(prof, 600, 50)
            at100 = estimate_remaining(prof, 600, 100)
        assert at0["seconds"] > at50["seconds"] > at100["seconds"]
        assert at100["seconds"] == 0
        assert "min" in at0["text"] or "s" in at0["text"]

    def test_percent_none_traite_comme_zero(self, app, _clean):
        with app.app_context():
            from transcria.workflow.profiles import get_profile
            from transcria.workflow.timing_service import estimate_remaining
            e = estimate_remaining(get_profile("dossier_qualite"), 600, None)
        assert e["seconds"] > 0


class TestQueueWait:
    def _entry(self, job_id, status, position, profile_id="dossier_qualite"):
        return SimpleNamespace(
            job_id=job_id, status=status, position=position,
            get_vram_profile=lambda pid=profile_id: {"processing_profile_id": pid},
        )

    def test_cumul_croissant_par_position(self, app, _clean, tmp_path):
        from transcria.jobs.filesystem import JobFilesystem
        from transcria.queue.wait_estimate import queue_wait_estimates
        cfg = {"storage": {"jobs_dir": str(tmp_path)}}
        for jid in ("j1", "j2", "j3"):
            JobFilesystem(str(tmp_path), jid).save_json(
                "metadata/audio_analysis.json", {"duration_seconds": 600})
        entries = [self._entry("j1", "waiting", 1),
                   self._entry("j2", "waiting", 2),
                   self._entry("j3", "waiting", 3)]
        with app.app_context():
            waits = queue_wait_estimates(cfg, entries)
        assert waits["j1"]["seconds"] == 0
        assert waits["j2"]["seconds"] > 0
        assert waits["j3"]["seconds"] > waits["j2"]["seconds"]

    def test_job_en_cours_compte_dans_lattente(self, app, _clean, tmp_path):
        from transcria.jobs.filesystem import JobFilesystem
        from transcria.queue.wait_estimate import queue_wait_estimates
        cfg = {"storage": {"jobs_dir": str(tmp_path)}}
        for jid in ("run", "wait"):
            JobFilesystem(str(tmp_path), jid).save_json(
                "metadata/audio_analysis.json", {"duration_seconds": 600})
        entries = [self._entry("run", "running", 0), self._entry("wait", "waiting", 1)]
        with app.app_context():
            waits = queue_wait_estimates(cfg, entries)
        assert waits["wait"]["seconds"] > 0

    def test_audio_absent_ne_casse_pas(self, app, _clean, tmp_path):
        from transcria.queue.wait_estimate import queue_wait_estimates
        cfg = {"storage": {"jobs_dir": str(tmp_path)}}
        entries = [self._entry("x", "waiting", 1)]
        with app.app_context():
            waits = queue_wait_estimates(cfg, entries)
        assert waits["x"]["seconds"] == 0

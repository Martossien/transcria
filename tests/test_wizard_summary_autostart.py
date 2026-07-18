"""Autostart du résumé dès la fin de l'upload (PISTES_AMELIORATION §5.6).

Opt-in (`workflow.summary_autostart.enabled`, défaut false) : enchaîne en fond
analyse → mise en FILE du résumé (SUMMARY_MODE — admission VRAM, all-in-one ET
frontal). Coutures substituées au CONSOMMATEUR (wizard_api), thread rendu
synchrone pour le déterminisme.
"""
from __future__ import annotations

import pytest
from builders import make_job

from transcria.jobs.models import JobState
from transcria.services.job_executor import SUMMARY_MODE
from transcria.web import wizard_api


class _ImmediateThread:
    started = 0

    def __init__(self, target=None, name=None, daemon=None):
        self._target = target

    def start(self):
        type(self).started += 1
        self._target()


class _FakeExecutor:
    def __init__(self):
        self.submitted: list = []

    def submit_process(self, job_id, audio_path, mode, vram_profile=None):
        self.submitted.append((job_id, mode, (vram_profile or {}).get("mode")))


@pytest.fixture
def uploaded_job(app, owner_id, tmp_path):
    from transcria.config import get_config
    from transcria.jobs.filesystem import JobFilesystem

    with app.app_context():
        job = make_job(owner_id, state=JobState.UPLOADED)
        fs = JobFilesystem(get_config()["storage"]["jobs_dir"], job.id)
        (fs.job_dir / "input").mkdir(parents=True, exist_ok=True)
        (fs.job_dir / "input" / "original.wav").write_bytes(b"RIFFxxxxWAVE")
        return job.id


def _wire(monkeypatch, app, executor, *, analyze_result=None):
    _ImmediateThread.started = 0
    monkeypatch.setattr(wizard_api.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(wizard_api, "get_job_executor", lambda: executor)
    analyzed = {"n": 0}

    def _fake_analyze(job_id, jobs_dir, cfg):
        analyzed["n"] += 1
        from transcria.jobs.store import JobStore
        JobStore.update_state(job_id, JobState.ANALYZED)
        return analyze_result if analyze_result is not None else {"duration_seconds": 12.0}

    monkeypatch.setattr(wizard_api.JobService, "analyze", staticmethod(_fake_analyze))
    return analyzed


def _cfg(enabled: bool) -> dict:
    from transcria.config import get_config
    cfg = get_config()
    cfg.setdefault("workflow", {})["summary_autostart"] = {"enabled": enabled}
    return cfg


class TestAutostart:
    def test_defaut_desactive_aucun_thread(self, app, uploaded_job, monkeypatch):
        executor = _FakeExecutor()
        with app.app_context():
            _wire(monkeypatch, app, executor)
            cfg = _cfg(False)
            with app.test_request_context():
                wizard_api._maybe_autostart_summary(cfg, uploaded_job)

        assert _ImmediateThread.started == 0
        assert executor.submitted == []

    def test_active_analyse_puis_enfile_le_resume(self, app, uploaded_job, monkeypatch):
        executor = _FakeExecutor()
        with app.app_context():
            analyzed = _wire(monkeypatch, app, executor)
            cfg = _cfg(True)
            with app.test_request_context():
                wizard_api._maybe_autostart_summary(cfg, uploaded_job)

        assert analyzed["n"] == 1                              # analyse d'abord
        assert len(executor.submitted) == 1
        job_id, mode, profile_mode = executor.submitted[0]
        assert (job_id, mode, profile_mode) == (uploaded_job, SUMMARY_MODE, "summary")

    def test_analyse_en_echec_nenfile_rien(self, app, uploaded_job, monkeypatch):
        executor = _FakeExecutor()
        with app.app_context():
            _wire(monkeypatch, app, executor, analyze_result={"error": "audio illisible"})
            cfg = _cfg(True)
            with app.test_request_context():
                wizard_api._maybe_autostart_summary(cfg, uploaded_job)

        assert executor.submitted == []

    def test_job_deja_analyse_ne_relance_pas_lanalyse(self, app, owner_id, monkeypatch):
        from transcria.jobs.store import JobStore

        executor = _FakeExecutor()
        with app.app_context():
            job = make_job(owner_id, state=JobState.SUMMARY_RUNNING)
            analyzed = _wire(monkeypatch, app, executor)
            cfg = _cfg(True)
            with app.test_request_context():
                wizard_api._maybe_autostart_summary(cfg, job.id)

        assert analyzed["n"] == 0                              # état ≠ UPLOADED → intact
        assert executor.submitted == []
        with app.app_context():
            assert JobStore.get_by_id(job.id).state == JobState.SUMMARY_RUNNING.value

    def test_entree_deja_en_file_pas_de_doublon(self, app, uploaded_job, monkeypatch):
        from types import SimpleNamespace

        executor = _FakeExecutor()
        with app.app_context():
            _wire(monkeypatch, app, executor)
            monkeypatch.setattr(
                wizard_api.QueueStore, "get_entry",
                staticmethod(lambda job_id: SimpleNamespace(mode=SUMMARY_MODE, status="waiting")))
            cfg = _cfg(True)
            with app.test_request_context():
                wizard_api._maybe_autostart_summary(cfg, uploaded_job)

        assert executor.submitted == []


class TestAnalyzeStateGuard:
    """La route analyze ne régresse plus l'état d'un job dont le résumé est en
    cours/fait (bug latent rendu probable par l'autostart)."""

    def test_analyze_sur_summary_running_est_idempotent(self, app, admin_client, owner_id):
        from transcria.config import get_config
        from transcria.jobs.filesystem import JobFilesystem
        from transcria.jobs.store import JobStore

        with app.app_context():
            job = make_job(owner_id, state=JobState.SUMMARY_RUNNING)
            fs = JobFilesystem(get_config()["storage"]["jobs_dir"], job.id)
            fs.save_json("metadata/audio_analysis.json", {"duration_seconds": 42.0})
            job_id = job.id

        r = admin_client.post(f"/api/jobs/{job_id}/analyze")

        assert r.status_code == 200
        assert r.get_json()["duration_seconds"] == 42.0        # analyse stockée servie
        with app.app_context():
            assert JobStore.get_by_id(job_id).state == JobState.SUMMARY_RUNNING.value

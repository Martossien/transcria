"""Pipeline reprenable (checkpoint/resume) — voir docs/PIPELINE_REPRISE.md.

Vérifie que `_run_pipeline_steps` saute les phases déjà faites (marqueur
`completed_phases` / artefact) et reprend à la première incomplète, sans re-travail.
"""
from __future__ import annotations

import pytest

from transcria.jobs.store import JobStore
from transcria.services.pipeline_service import PipelineService
from transcria.workflow import resume


def _cfg(tmp_path):
    return {
        "storage": {"jobs_dir": str(tmp_path / "jobs")},
        "workflow": {
            "enable_quality_mode": True,
            "arbitration_llm": {"model_id": "local/t", "enabled": True},
            "summary_llm": {"enabled": False},
        },
        "models": {"stt_backend": "cohere"},
    }


class _SL:
    def set_context(self, **k):
        pass

    def __getattr__(self, _name):
        return lambda *a, **k: None


def _instrument(svc, monkeypatch):
    """Remplace toutes les phases par des compteurs (succès, pas de GPU)."""
    calls: dict[str, int] = {}

    def _count(name, ret):
        def _fn(*a, **k):
            calls[name] = calls.get(name, 0) + 1
            return ret
        return _fn

    # Préprocess (représenté par preflight ; les autres renvoient le audio_path inchangé).
    monkeypatch.setattr(svc, "_run_audio_preflight", _count("preprocess", {}))
    monkeypatch.setattr(svc, "_run_audio_scene_analysis", lambda *a, **k: {})
    monkeypatch.setattr(svc, "_refresh_audio_quality_with_scene", lambda *a, **k: None)
    for m in ("_run_source_separation", "_run_audio_scene_filter",
              "_run_audio_denoise", "_run_audio_normalization"):
        monkeypatch.setattr(svc, m, lambda job, audio, *a, **k: audio)

    monkeypatch.setattr(svc.runner, "run_transcription", _count("transcription", {"segments": [1]}))
    monkeypatch.setattr(svc.runner, "run_diarization", _count("diarization", {"available": True}))
    monkeypatch.setattr(svc.runner, "run_correction", _count("correction", {"success": True}))
    monkeypatch.setattr(svc.runner, "run_final_review", _count("final_review", {"success": True}))
    monkeypatch.setattr(svc.runner, "run_quality_checks", _count("quality", {"success": True}))
    monkeypatch.setattr(svc.runner, "build_export", _count("export", {"success": True}))
    return calls


def test_fresh_run_executes_and_marks_all_phases(app, owner_id, monkeypatch, tmp_path):
    with app.app_context():
        cfg = _cfg(tmp_path)
        job = JobStore.create_job(owner_id, "Fresh")
        svc = PipelineService(cfg)
        calls = _instrument(svc, monkeypatch)

        result = svc._run_pipeline_steps(job, str(tmp_path / "a.wav"), "quality", _SL(), finalize_job_state=False)

        assert result.get("status") == "completed"
        # Toutes les phases exécutées une fois.
        for ph in ("preprocess", "transcription", "diarization", "correction", "final_review", "quality", "export"):
            assert calls.get(ph) == 1, ph
        done = resume.get_completed_phases(JobStore.get_by_id(job.id))
        assert set(done) == {"preprocess", "transcription", "diarization", "correction", "final_review", "quality", "export"}


def test_resume_skips_completed_phases(app, owner_id, monkeypatch, tmp_path):
    with app.app_context():
        cfg = _cfg(tmp_path)
        job = JobStore.create_job(owner_id, "Resume")
        # Simule un run précédent : préprocess + STT + diarisation déjà faits.
        # Le fichier prétraité DOIT exister localement : depuis le chantier stockage
        # partagé, un chemin mémorisé absent de ce disque (reprise sur un autre worker)
        # fait légitimement rejouer le préprocess.
        (tmp_path / "processed.wav").write_bytes(b"RIFFfake")
        resume.set_processed_audio_path(JobStore, job.id, str(tmp_path / "processed.wav"))
        for ph in ("preprocess", "transcription", "diarization"):
            resume.mark_phase_done(JobStore, job.id, ph)

        svc = PipelineService(cfg)
        calls = _instrument(svc, monkeypatch)

        result = svc._run_pipeline_steps(job, str(tmp_path / "a.wav"), "quality", _SL(), finalize_job_state=False)

        assert result.get("status") == "completed"
        # Phases déjà faites : NON rejouées.
        assert calls.get("preprocess") is None
        assert calls.get("transcription") is None
        assert calls.get("diarization") is None
        # Phases restantes : exécutées.
        assert calls.get("correction") == 1
        assert calls.get("quality") == 1
        assert calls.get("export") == 1


def test_resume_skips_transcription_via_artifact(app, owner_id, monkeypatch, tmp_path):
    with app.app_context():
        from transcria.jobs.filesystem import JobFilesystem
        cfg = _cfg(tmp_path)
        job = JobStore.create_job(owner_id, "Artifact")
        # Artefact présent SANS marqueur (run interrompu avant l'inscription) → rétro-remplissage.
        resume.mark_phase_done(JobStore, job.id, "preprocess")
        resume.set_processed_audio_path(JobStore, job.id, str(tmp_path / "a.wav"))
        fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
        fs.save_text("metadata/transcription.srt", "1\n00:00:00,000 --> 00:00:01,000\nok\n")

        svc = PipelineService(cfg)
        calls = _instrument(svc, monkeypatch)

        svc._run_pipeline_steps(job, str(tmp_path / "a.wav"), "quality", _SL(), finalize_job_state=False)
        assert calls.get("transcription") is None  # sauté via l'artefact
        # Et le marqueur a été rétro-rempli.
        assert "transcription" in resume.get_completed_phases(JobStore.get_by_id(job.id))


def test_reprocess_route_resets_resume_state(app, monkeypatch):
    """Régression : /reprocess d'un job complété doit VIDER l'état de reprise, sinon le
    pipeline reprenable sauterait toutes les phases (no-op)."""
    from transcria.jobs.filesystem import JobFilesystem
    from transcria.jobs.models import JobState

    submits = []

    class _Stub:
        def submit_process(self, job_id, audio_path, mode, **kwargs):
            submits.append(mode)
            return {"accepted": True}

    monkeypatch.setattr("transcria.web.routes.get_job_executor", lambda: _Stub())

    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "admin-change-me"}, follow_redirects=True)

    with app.app_context():
        from transcria.auth.store import UserStore
        from transcria.config import get_config
        admin = UserStore.get_by_username("admin")
        job = JobStore.create_job(admin.id, "Reprocess")
        JobStore.update_state(job.id, JobState.EXPORT_READY)
        for ph in ("preprocess", "transcription", "diarization", "correction", "quality", "export"):
            resume.mark_phase_done(JobStore, job.id, ph)
        fs = JobFilesystem(get_config()["storage"]["jobs_dir"], job.id)
        (fs.job_dir / "input").mkdir(parents=True, exist_ok=True)
        (fs.job_dir / "input" / "original.wav").write_text("fake")
        job_id = job.id

    resp = client.post(f"/api/jobs/{job_id}/reprocess", json={"mode": "fast"})
    assert resp.status_code == 202
    assert submits == ["fast"]
    with app.app_context():
        assert resume.get_completed_phases(JobStore.get_by_id(job_id)) == []  # état de reprise vidé


def test_reset_clears_resume_state(app, owner_id, tmp_path):
    with app.app_context():
        job = JobStore.create_job(owner_id, "Reset")
        resume.mark_phase_done(JobStore, job.id, "transcription")
        resume.set_processed_audio_path(JobStore, job.id, "/x.wav")
        assert resume.get_completed_phases(JobStore.get_by_id(job.id)) == ["transcription"]

        resume.reset_resume_state(JobStore, job.id)
        fresh = JobStore.get_by_id(job.id)
        assert resume.get_completed_phases(fresh) == []
        assert resume.get_processed_audio_path(fresh) is None

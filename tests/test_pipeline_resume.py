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
        from transcria.jobs.filesystem import JobFilesystem
        cfg = _cfg(tmp_path)
        job = JobStore.create_job(owner_id, "Resume")
        # Simule un run précédent : préprocess + STT + diarisation déjà faits.
        # Le fichier prétraité DOIT exister localement : depuis le chantier stockage
        # partagé, un chemin mémorisé absent de ce disque (reprise sur un autre worker)
        # fait légitimement rejouer le préprocess. Depuis la provenance v2, l'artefact
        # déclaré d'une phase marquée doit lui aussi exister (un STT fait a son SRT).
        (tmp_path / "processed.wav").write_bytes(b"RIFFfake")
        resume.set_processed_audio_path(JobStore, job.id, str(tmp_path / "processed.wav"))
        fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
        fs.save_text("metadata/transcription.srt", "1\n00:00:00,000 --> 00:00:01,000\nok\n")
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

    monkeypatch.setattr("transcria.web.processing_api.get_job_executor", lambda: _Stub())

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


def _instrument_artifacts(svc, monkeypatch, fs, outputs):
    """Phases qui ÉCRIVENT leurs artefacts — la provenance v2 empreinte ces fichiers.

    `outputs` est un dict mutable relu à chaque exécution : le test pilote le contenu
    produit par chaque run (sortie différente = amont rejoué, identique = idempotent).
    """
    calls: dict[str, int] = {}

    def _count(name, fn):
        def _wrapped(*a, **k):
            calls[name] = calls.get(name, 0) + 1
            return fn()
        return _wrapped

    monkeypatch.setattr(svc, "_run_audio_preflight", _count("preprocess", lambda: {}))
    monkeypatch.setattr(svc, "_run_audio_scene_analysis", lambda *a, **k: {})
    monkeypatch.setattr(svc, "_refresh_audio_quality_with_scene", lambda *a, **k: None)
    for m in ("_run_source_separation", "_run_audio_scene_filter",
              "_run_audio_denoise", "_run_audio_normalization"):
        monkeypatch.setattr(svc, m, lambda job, audio, *a, **k: audio)

    def _stt():
        fs.save_text("metadata/transcription.srt", outputs["srt"])
        return {"segments": [1]}

    def _corr():
        fs.save_text("metadata/transcription_corrigee.srt", outputs["corrigee"])
        return {"success": True}

    def _qual():
        fs.save_json("quality/quality_report.json", {"score": outputs.get("score", 97)})
        return {"success": True}

    monkeypatch.setattr(svc.runner, "run_transcription", _count("transcription", _stt))
    monkeypatch.setattr(svc.runner, "run_diarization", _count("diarization", lambda: {"available": True}))
    monkeypatch.setattr(svc.runner, "run_correction", _count("correction", _corr))
    monkeypatch.setattr(svc.runner, "run_final_review", _count("final_review", lambda: {"success": True}))
    monkeypatch.setattr(svc.runner, "run_quality_checks", _count("quality", _qual))
    monkeypatch.setattr(svc.runner, "build_export", _count("export", lambda: {"success": True}))
    return calls


def _full_run(app_cfg, job, svc_calls, tmp_path):
    svc, calls = svc_calls
    result = svc._run_pipeline_steps(job, str(tmp_path / "a.wav"), "quality", _SL(), finalize_job_state=False)
    assert result.get("status") == "completed"
    return calls


class TestProvenance:
    """Provenance v2 : un skip n'est légitime que si les ENTRÉES de la phase n'ont pas
    bougé depuis son checkpoint (empreintes sha256). Régression du job 4bda98cb :
    correction rejouée → rapport qualité skippé sur artefact périmé (97/100 calculé
    sur le SRT brut d'un run précédent)."""

    def _setup(self, app, owner_id, monkeypatch, tmp_path, outputs):
        from transcria.jobs.filesystem import JobFilesystem
        cfg = _cfg(tmp_path)
        (tmp_path / "a.wav").write_bytes(b"RIFFfake")
        job = JobStore.create_job(owner_id, "Provenance")
        fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
        svc = PipelineService(cfg)
        calls = _instrument_artifacts(svc, monkeypatch, fs, outputs)
        return job, fs, svc, calls

    def test_upstream_rerun_invalidates_downstream(self, app, owner_id, monkeypatch, tmp_path):
        """LE bug : l'amont change → les phases aval marquées faites se RÉ-EXÉCUTENT."""
        with app.app_context():
            outputs = {"srt": "v1 brut", "corrigee": "v1 corrigée"}
            job, fs, svc, calls = self._setup(app, owner_id, monkeypatch, tmp_path, outputs)
            _full_run(None, job, (svc, calls), tmp_path)
            calls.clear()

            # Le SRT brut change (STT rejoué / source modifiée hors marqueurs) et la
            # correction produira une sortie différente.
            fs.save_text("metadata/transcription.srt", "v2 brut")
            outputs["corrigee"] = "v2 corrigée"

            _full_run(None, job, (svc, calls), tmp_path)
            # transcription : marqueur + artefact + entrées vides → skip (pas d'empreinte audio).
            assert calls.get("transcription") is None
            assert calls.get("diarization") is None
            # correction : empreinte du SRT brut ≠ → invalidée → rejouée.
            assert calls.get("correction") == 1
            # quality/export : leurs entrées (SRT corrigé…) ont changé → rejouées, pas de
            # rapport périmé skippé.
            assert calls.get("quality") == 1
            assert calls.get("export") == 1
            done = resume.get_completed_phases(JobStore.get_by_id(job.id))
            assert {"correction", "quality", "export"} <= set(done)

    def test_byte_identical_rerun_keeps_downstream_skipped(self, app, owner_id, monkeypatch, tmp_path):
        """Sémantique exacte : amont rejoué à sortie byte-identique → l'aval skippe."""
        with app.app_context():
            outputs = {"srt": "v1 brut", "corrigee": "v1 corrigée"}
            job, fs, svc, calls = self._setup(app, owner_id, monkeypatch, tmp_path, outputs)
            _full_run(None, job, (svc, calls), tmp_path)
            calls.clear()

            # La correction doit se rejouer (artefact supprimé) mais reproduit le même octet.
            (fs.job_dir / "metadata" / "transcription_corrigee.srt").unlink()

            _full_run(None, job, (svc, calls), tmp_path)
            assert calls.get("correction") == 1
            # Entrées de quality/export inchangées (corrigé identique) → skip légitime.
            assert calls.get("quality") is None
            assert calls.get("export") is None

    def test_legacy_marker_without_fingerprints_reruns(self, app, owner_id, monkeypatch, tmp_path):
        """Marqueur sans empreintes (job en vol au déploiement v2) : doute → re-run."""
        with app.app_context():
            outputs = {"srt": "v1 brut", "corrigee": "v1 corrigée"}
            job, fs, svc, calls = self._setup(app, owner_id, monkeypatch, tmp_path, outputs)
            (tmp_path / "processed.wav").write_bytes(b"RIFFfake")
            resume.set_processed_audio_path(JobStore, job.id, str(tmp_path / "processed.wav"))
            fs.save_text("metadata/transcription.srt", "v1 brut")
            fs.save_text("metadata/transcription_corrigee.srt", "v1 corrigée")
            # Marquage legacy : sans empreintes.
            for ph in ("preprocess", "transcription", "correction"):
                resume.mark_phase_done(JobStore, job.id, ph)

            _full_run(None, job, (svc, calls), tmp_path)
            # transcription : entrées vides déclarées → marqueur legacy suffit.
            assert calls.get("transcription") is None
            # correction : entrées déclarées sans empreintes enregistrées → rejouée.
            assert calls.get("correction") == 1

    def test_same_content_different_mtime_still_skips(self, app, owner_id, monkeypatch, tmp_path):
        """Cross-machine (split pg) : le pull rematérialise sans préserver les mtimes —
        la fraîcheur est par CONTENU, un même octet à mtime différent skippe toujours."""
        with app.app_context():
            outputs = {"srt": "v1 brut", "corrigee": "v1 corrigée"}
            job, fs, svc, calls = self._setup(app, owner_id, monkeypatch, tmp_path, outputs)
            _full_run(None, job, (svc, calls), tmp_path)
            calls.clear()

            # Simule la rematérialisation : mêmes contenus, mtimes neufs.
            for rel in ("metadata/transcription.srt", "metadata/transcription_corrigee.srt"):
                content = (fs.job_dir / rel).read_text(encoding="utf-8")
                fs.save_text(rel, content)

            _full_run(None, job, (svc, calls), tmp_path)
            assert calls == {}  # tout est sauté : rien n'a changé en contenu

    def test_quality_artifact_without_marker_reruns(self, app, owner_id, monkeypatch, tmp_path):
        """Plus de rétro-remplissage aveugle : un quality_report.json orphelin (sans
        marqueur) ne vaut pas « phase faite » — il peut dater d'un autre état du SRT."""
        with app.app_context():
            outputs = {"srt": "v1 brut", "corrigee": "v1 corrigée"}
            job, fs, svc, calls = self._setup(app, owner_id, monkeypatch, tmp_path, outputs)
            fs.save_json("quality/quality_report.json", {"score": 97})  # artefact périmé orphelin

            _full_run(None, job, (svc, calls), tmp_path)
            assert calls.get("quality") == 1  # recalculé, pas adopté

    def test_invalidation_unmarks_phase_in_db_before_execution(self, app, owner_id, monkeypatch, tmp_path):
        """L'invalidation est PERSISTÉE avant d'exécuter : si un vram_wait coupe la chaîne
        à cet endroit, l'admission du re-queue voit la phase comme restante (VRAM LLM
        comptée), et l'UI ne prétend pas qu'elle est faite."""
        with app.app_context():
            outputs = {"srt": "v1 brut", "corrigee": "v1 corrigée"}
            job, fs, svc, calls = self._setup(app, owner_id, monkeypatch, tmp_path, outputs)
            _full_run(None, job, (svc, calls), tmp_path)

            # L'amont change, et la correction tombe en pénurie VRAM au re-run.
            fs.save_text("metadata/transcription.srt", "v2 brut")
            monkeypatch.setattr(
                svc.runner, "run_correction",
                lambda *a, **k: {"vram_wait": True, "required_mb": 16000, "phase": "llm_arbitration"},
            )
            result = svc._run_pipeline_steps(job, str(tmp_path / "a.wav"), "quality", _SL(), finalize_job_state=False)
            assert result.get("vram_wait")
            done = resume.get_completed_phases(JobStore.get_by_id(job.id))
            assert "correction" not in done  # marqueur retiré EN BASE avant l'exécution
            assert "transcription" in done   # l'amont sauté reste marqué

    def test_transient_final_review_skip_not_marked_done(self, app, owner_id, monkeypatch, tmp_path):
        """Relecture finale (best-effort) sautée pour cause TRANSITOIRE (LLM occupée par
        un autre job) : enregistrée `skipped`, JAMAIS marquée faite (sinon jamais rejouée
        = perte silencieuse de l'harmonisation), pipeline complété malgré tout."""
        with app.app_context():
            outputs = {"srt": "v1 brut", "corrigee": "v1 corrigée"}
            job, fs, svc, calls = self._setup(app, owner_id, monkeypatch, tmp_path, outputs)
            monkeypatch.setattr(
                svc.runner, "run_final_review",
                lambda *a, **k: {"success": True, "skipped": True, "retryable": True, "reason": "llm_busy"},
            )
            _full_run(None, job, (svc, calls), tmp_path)
            j = JobStore.get_by_id(job.id)
            done = resume.get_completed_phases(j)
            assert "final_review" not in done                         # PAS gravée faite
            assert {"correction", "quality", "export"} <= set(done)   # le reste l'est
            assert resume.get_skipped_phases(j) == {"final_review": "llm_busy"}

    def test_permanent_final_review_skip_is_marked_done(self, app, owner_id, monkeypatch, tmp_path):
        """Skip PERMANENT (rien à relire) : marqué fait (légitime), pas dans skipped_phases —
        la distinction transitoire (retryable) / permanent est respectée."""
        with app.app_context():
            outputs = {"srt": "v1 brut", "corrigee": "v1 corrigée"}
            job, fs, svc, calls = self._setup(app, owner_id, monkeypatch, tmp_path, outputs)
            monkeypatch.setattr(
                svc.runner, "run_final_review",
                lambda *a, **k: {"success": True, "skipped": True, "reason": "nothing_to_review"},
            )
            _full_run(None, job, (svc, calls), tmp_path)
            j = JobStore.get_by_id(job.id)
            assert "final_review" in resume.get_completed_phases(j)
            assert resume.get_skipped_phases(j) == {}


class TestSkippedPhases:
    """Suivi des skips transitoires (resume.mark_phase_skipped / get_skipped_phases)."""

    def test_mark_skipped_records_reason_without_completing(self, app, owner_id):
        with app.app_context():
            job = JobStore.create_job(owner_id, "skip")
            resume.mark_phase_skipped(JobStore, job.id, "final_review", "llm_busy")
            j = JobStore.get_by_id(job.id)
            assert "final_review" not in resume.get_completed_phases(j)
            assert resume.get_skipped_phases(j) == {"final_review": "llm_busy"}

    def test_mark_skipped_removes_stale_completed_marker(self, app, owner_id):
        """Si la phase avait été (à tort) marquée faite, un skip transitoire l'en retire."""
        with app.app_context():
            job = JobStore.create_job(owner_id, "skip2")
            resume.mark_phase_done(JobStore, job.id, "final_review", {"context/x": "h"})
            resume.mark_phase_skipped(JobStore, job.id, "final_review", "vram_insufficient")
            j = JobStore.get_by_id(job.id)
            assert "final_review" not in resume.get_completed_phases(j)
            assert resume.get_phase_fingerprints(j).get("final_review") is None
            assert resume.get_skipped_phases(j)["final_review"] == "vram_insufficient"

    def test_success_clears_skipped_flag(self, app, owner_id):
        with app.app_context():
            job = JobStore.create_job(owner_id, "skip3")
            resume.mark_phase_skipped(JobStore, job.id, "final_review", "llm_busy")
            resume.mark_phase_done(JobStore, job.id, "final_review", {"context/x": "h"})
            j = JobStore.get_by_id(job.id)
            assert "final_review" in resume.get_completed_phases(j)
            assert resume.get_skipped_phases(j) == {}

    def test_get_skipped_empty_by_default(self, app, owner_id):
        with app.app_context():
            job = JobStore.create_job(owner_id, "skip4")
            assert resume.get_skipped_phases(JobStore.get_by_id(job.id)) == {}


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

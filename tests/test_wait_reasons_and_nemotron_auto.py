"""Incident 2026-11-07 (job 9b63a5c7) : résumé lancé deux fois en parallèle, puis traitement
complet en attente MUETTE pendant une heure derrière 2 Go de VRAM que des cartes pleines
ne libéreraient jamais — alors que la phase sait tourner sur CPU.

Quatre garde-fous : l'analyse ne rétrograde plus l'état d'un job qui a avancé ; l'admission
n'exige aucune VRAM pour Nemotron 3 en « auto » ; la diarisation finale bascule sur CPU faute
de carte ; un job différé dit pourquoi (état d'exécution, API de statut, journal une seule fois).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from builders import make_config

from transcria.services import pipeline_admission
from transcria.stt import diarizer_factory as df
from transcria.workflow import transitions
from transcria.workflow.profiles import get_profile

# ── Admission Nemotron « auto » ──────────────────────────────────────────────

def _nemotron_cfg(mode: str) -> dict:
    return make_config({"models": {"diarization_backend": "nemotron_diar"},
                        "nemotron_diar": {"backend": mode}})


def test_admission_n_exige_aucune_vram_pour_nemotron_auto(monkeypatch):
    monkeypatch.setattr(df, "cli_is_cpu_only", lambda cli: False)        # binaire CUDA présent
    cfg = _nemotron_cfg("auto")
    assert df.get_diarizer_vram_mb("nemotron_diar", cfg) == 2000           # la PHASE tente la carte
    assert df.get_diarizer_vram_mb("nemotron_diar", cfg, for_admission=True) == 0
    assert df.get_diarizer_vram_mb("nemotron_diar", cfg, at_upload=True) == 0
    est = pipeline_admission.estimate_profile_resources(cfg, get_profile("dossier_qualite"))
    assert est["phases"]["diarization"] == 0


def test_admission_garde_la_vram_en_cuda_explicite(monkeypatch):
    monkeypatch.setattr(df, "cli_is_cpu_only", lambda cli: False)
    cfg = _nemotron_cfg("cuda")
    assert df.get_diarizer_vram_mb("nemotron_diar", cfg, for_admission=True) == 2000
    assert df.get_diarizer_vram_mb("nemotron_diar", _nemotron_cfg("cpu"), for_admission=True) == 0


def test_bascule_cpu_seulement_pour_nemotron_auto_ou_cpu(monkeypatch):
    monkeypatch.setattr(df, "cli_is_cpu_only", lambda cli: False)
    assert df.nemotron_auto_cpu_fallback("nemotron_diar", _nemotron_cfg("auto")) is True
    assert df.nemotron_auto_cpu_fallback("nemotron_diar", _nemotron_cfg("cpu")) is True
    assert df.nemotron_auto_cpu_fallback("nemotron_diar", _nemotron_cfg("cuda")) is False
    assert df.nemotron_auto_cpu_fallback("pyannote", make_config()) is False
    assert df.nemotron_auto_cpu_fallback("sortformer", make_config()) is False


# ── Raison d'attente ─────────────────────────────────────────────────────────

def test_mark_execution_deferred_puis_effacee_au_demarrage(app, owner_id):
    from transcria.jobs.store import JobStore
    from transcria.services.job_service import JobService

    with app.app_context():
        job_id = JobService.create(owner_id, "différé")["job_id"]
        transitions.mark_execution_queued(job_id, "quality")
        transitions.mark_execution_deferred(job_id, reason=transitions.DEFERRED_LOCAL_VRAM, required_mb=2000)
        execution = JobStore.get_by_id(job_id).get_extra_data()["execution"]
        assert execution["status"] == "queued"                                # le statut ne change pas
        assert execution["deferred_reason"] == "local_vram" and execution["deferred_required_mb"] == 2000
        transitions.mark_execution_started(job_id)
        execution = JobStore.get_by_id(job_id).get_extra_data()["execution"]
        assert execution["deferred_reason"] is None and execution["status"] == "running"


def test_status_api_expose_la_raison_d_attente(app, admin_client, owner_id, monkeypatch):
    from transcria.services.job_service import JobService
    from transcria.web import processing_api

    with app.app_context():
        job_id = JobService.create(owner_id, "différé api")["job_id"]
        transitions.mark_execution_queued(job_id, "quality")
        transitions.mark_execution_deferred(job_id, reason=transitions.DEFERRED_LOCAL_VRAM, required_mb=2000)
    monkeypatch.setattr(processing_api, "_queue_wait_info", lambda job: {"queue_position": 1})
    payload = admin_client.get(f"/api/jobs/{job_id}/status").get_json()
    assert payload["queue_position"] == 1
    assert payload["deferred_reason"] == "local_vram" and payload["deferred_required_mb"] == 2000


def test_scheduler_note_la_raison_une_seule_fois(app, tmp_path, monkeypatch, caplog):
    from transcria.queue.scheduler import QueueScheduler

    cfg = make_config({"storage": {"jobs_dir": str(tmp_path / "jobs")}})
    with app.app_context():
        scheduler = QueueScheduler(app, cfg, lambda *a: None)
        recorded: list[tuple] = []
        monkeypatch.setattr("transcria.queue.scheduler.mark_execution_deferred",
                            lambda job_id, *, reason, required_mb=0: recorded.append((job_id, reason, required_mb)))
        import logging
        with caplog.at_level(logging.INFO, logger="transcria.queue.scheduler"):
            scheduler._note_deferral("job-1", "local_vram", 2000, "différé")
            scheduler._note_deferral("job-1", "local_vram", 2000, "différé")   # même raison : silence
            scheduler._note_deferral("job-1", "llm_vram", 0, "différé LLM")     # raison nouvelle : noté
        scheduler._executor.shutdown(wait=True)
    assert recorded == [("job-1", "local_vram", 2000), ("job-1", "llm_vram", 0)]
    assert sum(1 for r in caplog.records if r.getMessage().startswith("différé")) == 2


# ── Formulaire ───────────────────────────────────────────────────────────────

def test_formulaire_expose_le_calcul_nemotron():
    from transcria.web.config_form import CONFIG_FORM_SECTIONS

    field = next(f for sec in CONFIG_FORM_SECTIONS for f in sec["fields"] if f["path"] == "nemotron_diar.backend")
    assert field["options"] == ["auto", "cuda", "cpu"]
    assert set(field["option_labels"]) == {"auto", "cuda", "cpu"}
    assert "sinon CPU" in str(field["option_labels"]["auto"])


def _job_state_after_analyze(app, owner_id, start_state):
    """Analyse sur un job déjà passé à `start_state` : rend l'état FINAL (le fichier est mocké)."""
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    from transcria.jobs.models import JobState
    from transcria.jobs.store import JobStore
    from transcria.services.job_service import JobService

    with tempfile.TemporaryDirectory() as d:
        fake_audio = Path(d) / "test.wav"
        fake_audio.write_bytes(b"RIFF")
        with app.app_context():
            job_id = JobService.create(owner_id, "analyse tardive")["job_id"]
            JobStore.update_state(job_id, JobState.UPLOADED)
            if start_state != JobState.UPLOADED:
                JobStore.update_state(job_id, JobState.ANALYZED)
                if start_state != JobState.ANALYZED:
                    JobStore.update_state(job_id, start_state)
            with patch("transcria.services.job_service.JobFilesystem") as MockFs:
                MockFs.return_value.get_original_audio_path.return_value = fake_audio
                MockFs.return_value.save_json.return_value = None
                with patch("transcria.audio.analyzer.AudioAnalyzer.analyze", return_value={"duration_seconds": 120}):
                    with patch("transcria.audio.preflight.AudioPreflightAnalyzer") as MockPf:
                        MockPf.return_value.enabled = False
                        with patch("transcria.quality.audio_quality.AudioQualityEvaluator") as MockQe:
                            MockQe.return_value.evaluate.return_value = {}
                            JobService.analyze(job_id, d, {})
            return JobStore.get_by_id(job_id).state


def test_l_analyse_ne_retrograde_pas_un_resume_en_cours(app, owner_id):
    from transcria.jobs.models import JobState

    assert _job_state_after_analyze(app, owner_id, JobState.UPLOADED) == JobState.ANALYZED.value
    assert _job_state_after_analyze(app, owner_id, JobState.SUMMARY_RUNNING) == JobState.SUMMARY_RUNNING.value


def test_payload_deferred_json_serialisable():
    assert json.dumps({"deferred_reason": transitions.DEFERRED_REMOTE_VRAM, "x": SimpleNamespace}, default=str)

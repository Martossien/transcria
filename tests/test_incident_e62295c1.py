"""Suites de l'incident e62295c1 : échec silencieux LLM (Bug #1) + déblocage VRAM (Bug #2).

- Bug #1 : opencode « 0 texte » → détecté (`_summary_produced=False`), retry ≤ 3, puis
  `summary_llm_failed` (pas de SUMMARY_DONE, meeting_context non corrompu, relançable).
- Bug #2 : arrêt de NOTRE LLM d'arbitrage inactive pour libérer la VRAM d'un STT bloqué.
"""
from __future__ import annotations

import os
import time
from types import SimpleNamespace

from transcria.gpu.opencode_runner import OpenCodeRunner
from transcria.jobs.models import JobState
from transcria.jobs.store import JobStore
from transcria.workflow.runner import WorkflowRunner


def _cfg(tmp_path, **wf):
    workflow = {
        "enable_quick_summary": True,
        "enable_speaker_detection": False,
        "summary_llm": {"enabled": True, "model_id": "local/test-llm"},
        "arbitration_llm": {"model_id": "local/test-llm"},
    }
    workflow.update(wf)
    return {
        "storage": {"jobs_dir": str(tmp_path / "jobs")},
        "workflow": workflow,
        "services": {"arbitrage_script": "/bin/true", "stop_script": "/bin/true", "arbitrage_llm_port": 8080},
        "models": {"stt_backend": "cohere"},
    }


# ---------------------------------------------------------------------------
# Bug #1 — OpenCodeRunner.run_summary : détection « 0 texte » via mtime de summary.md
# ---------------------------------------------------------------------------

def _make_runner(work_dir):
    return OpenCodeRunner(str(work_dir), model="local/test-llm", config={"workflow": {"arbitration_llm": {"model_id": "local/test-llm"}}})


def test_run_summary_detects_unproduced_when_placeholder_untouched(tmp_path, monkeypatch):
    work = tmp_path / "summary"
    work.mkdir(parents=True)
    placeholder = work / "summary.md"
    placeholder.write_text("# Résumé de contrôle\n\nRésumé de contrôle indisponible (LLM non configurée).\n", encoding="utf-8")
    # Placeholder « ancien » : tout (ré)écriture serait strictement postérieure.
    os.utime(placeholder, (time.time() - 60, time.time() - 60))
    (work / "quick_transcript.txt").write_text("blah", encoding="utf-8")

    runner = _make_runner(work)
    # opencode « réussit » mais ne produit aucun texte et ne réécrit pas summary.md.
    monkeypatch.setattr(OpenCodeRunner, "run", lambda self, instr, pf, timeout=600: {
        "success": True, "output": "", "files": [], "events_count": 9, "tool_calls": 3,
    })

    parsed = runner.run_summary(str(work / "quick_transcript.txt"), str(work / "ctx.yaml"), str(work / "diar.md"))
    assert parsed["_summary_produced"] is False
    assert parsed["summary_text"] == "Résumé indisponible."


def test_run_summary_detects_produced_when_opencode_rewrites(tmp_path, monkeypatch):
    work = tmp_path / "summary"
    work.mkdir(parents=True)
    placeholder = work / "summary.md"
    placeholder.write_text("placeholder", encoding="utf-8")
    os.utime(placeholder, (time.time() - 60, time.time() - 60))

    runner = _make_runner(work)
    structured = (
        "# Résumé\n\n**Titre suggéré :** Réunion test\n\n"
        "## Participants probables\n- SPEAKER_00 : Animateur\n\n## Synthèse\nOK\n"
    )

    def fake_run(self, instr, pf, timeout=600):
        # opencode réécrit summary.md (mtime postérieur au placeholder).
        (work / "summary.md").write_text(structured, encoding="utf-8")
        return {"success": True, "output": "", "files": [str(work / "summary.md")], "events_count": 12, "tool_calls": 4}

    monkeypatch.setattr(OpenCodeRunner, "run", fake_run)
    parsed = runner.run_summary(str(work / "qt.txt"), str(work / "ctx.yaml"), str(work / "diar.md"))
    assert parsed["_summary_produced"] is True
    assert "Réunion test" in parsed["summary_text"]


# ---------------------------------------------------------------------------
# Bug #1 — _run_llm_summary : retry ≤ 3 puis summary_llm_failed
# ---------------------------------------------------------------------------

def test_llm_summary_retries_three_times_then_marks_failed(app, owner_id, monkeypatch, tmp_path):
    with app.app_context():
        cfg = _cfg(tmp_path)
        job = JobStore.create_job(owner_id, "Retry")
        runner = WorkflowRunner(JobStore, cfg)

        monkeypatch.setattr(runner.allocator, "try_acquire_llm", lambda *a, **k: True)
        monkeypatch.setattr(runner.allocator, "release_llm", lambda *a, **k: None)
        monkeypatch.setattr(runner.vram, "is_arbitrage_llm_running", lambda: True)
        monkeypatch.setattr(runner.vram, "ensure_arbitrage_llm_ready", lambda **k: True)
        monkeypatch.setattr(WorkflowRunner, "_materialize_meeting_invite", staticmethod(lambda fs, job: None))

        calls = {"n": 0}

        def fake_run_summary(self, *a, **k):
            calls["n"] += 1
            return {"_summary_produced": False, "summary_text": "Résumé indisponible."}

        monkeypatch.setattr(OpenCodeRunner, "run_summary", fake_run_summary)

        result = {"transcript_text": "du texte"}
        runner._run_llm_summary(job, result, cfg, _DummySL())

        assert calls["n"] == 3                      # exactement 3 tentatives
        assert result.get("summary_llm_failed") is True


def test_llm_summary_succeeds_first_try(app, owner_id, monkeypatch, tmp_path):
    with app.app_context():
        cfg = _cfg(tmp_path)
        job = JobStore.create_job(owner_id, "OK")
        runner = WorkflowRunner(JobStore, cfg)
        monkeypatch.setattr(runner.allocator, "try_acquire_llm", lambda *a, **k: True)
        monkeypatch.setattr(runner.allocator, "release_llm", lambda *a, **k: None)
        monkeypatch.setattr(runner.vram, "is_arbitrage_llm_running", lambda: True)
        monkeypatch.setattr(runner.vram, "ensure_arbitrage_llm_ready", lambda **k: True)
        monkeypatch.setattr(WorkflowRunner, "_materialize_meeting_invite", staticmethod(lambda fs, job: None))
        monkeypatch.setattr(WorkflowRunner, "_apply_llm_suggestions", lambda self, fs, result, parsed, sl: None)

        calls = {"n": 0}

        def fake_run_summary(self, *a, **k):
            calls["n"] += 1
            # Un résumé EXPLOITABLE : produit ET au moins un champ critique extrait.
            return {"_summary_produced": True, "title_suggere": "Titre", "summary_text": "vrai résumé"}

        monkeypatch.setattr(OpenCodeRunner, "run_summary", fake_run_summary)
        result = {"transcript_text": "du texte"}
        runner._run_llm_summary(job, result, cfg, _DummySL())
        assert calls["n"] == 1
        assert "summary_llm_failed" not in result


def test_llm_summary_malformed_output_retries_then_fails(app, owner_id, monkeypatch, tmp_path):
    # Chasse aux bugs (batch E2E 2026-07-05) : un résumé « produit » mais MALFORMÉ
    # (gabarit non suivi → aucun champ critique) passait le check `_summary_produced` et
    # était accepté, cassant tout le parsing aval (relecture finale/DOCX). Il doit
    # désormais déclencher un retry, puis un échec relançable après 3 tentatives.
    with app.app_context():
        cfg = _cfg(tmp_path)
        job = JobStore.create_job(owner_id, "Malformé")
        runner = WorkflowRunner(JobStore, cfg)
        monkeypatch.setattr(runner.allocator, "try_acquire_llm", lambda *a, **k: True)
        monkeypatch.setattr(runner.allocator, "release_llm", lambda *a, **k: None)
        monkeypatch.setattr(runner.vram, "is_arbitrage_llm_running", lambda: True)
        monkeypatch.setattr(runner.vram, "ensure_arbitrage_llm_ready", lambda **k: True)
        monkeypatch.setattr(WorkflowRunner, "_materialize_meeting_invite", staticmethod(lambda fs, job: None))
        monkeypatch.setattr(WorkflowRunner, "_apply_llm_suggestions", lambda self, fs, result, parsed, sl: None)

        calls = {"n": 0}

        def fake_run_summary(self, *a, **k):
            calls["n"] += 1
            # produit=True mais aucun champ critique (titre/type/sujet vides) → inexploitable.
            return {"_summary_produced": True, "title_suggere": "", "type_suggere": "",
                    "sujet_suggere": "", "summary_text": "Now I have all the info. Let me write..."}

        monkeypatch.setattr(OpenCodeRunner, "run_summary", fake_run_summary)
        result = {"transcript_text": "du texte"}
        runner._run_llm_summary(job, result, cfg, _DummySL())
        assert calls["n"] == 3
        assert result.get("summary_llm_failed") is True


# ---------------------------------------------------------------------------
# Bug #1 — run_summary : échec LLM ⇒ pas de SUMMARY_DONE, drapeau posé, relançable
# ---------------------------------------------------------------------------

def test_run_summary_blocks_relaunchable_on_llm_failure(app, owner_id, monkeypatch, tmp_path):
    with app.app_context():
        cfg = _cfg(tmp_path)
        job = JobStore.create_job(owner_id, "Bloqué")
        JobStore.update_state(job.id, JobState.ANALYZED)
        prior = JobStore.get_by_id(job.id).state
        runner = WorkflowRunner(JobStore, cfg)

        monkeypatch.setattr(WorkflowRunner, "_run_quick_transcription",
                            lambda self, job, audio, config, sl: {"transcript_text": "t", "segment_count": 1})
        monkeypatch.setattr(WorkflowRunner, "_run_audio_scene_before_participants",
                            lambda self, job, audio, config, sl: {})
        monkeypatch.setattr(WorkflowRunner, "_run_pyannote_after_transcription",
                            lambda self, job, audio, config: None)

        def fake_llm(self, job, result, config, sl):
            result["summary_llm_failed"] = True

        monkeypatch.setattr(WorkflowRunner, "_run_llm_summary", fake_llm)

        result = runner.run_summary(job, str(tmp_path / "a.wav"), cfg)
        assert result.get("summary_llm_failed") is True

        updated = JobStore.get_by_id(job.id)
        assert updated.state != JobState.SUMMARY_DONE.value
        assert updated.state == prior                       # relançable
        assert updated.get_extra_data().get("summary_llm_failed", {}).get("attempts") == 3


# ---------------------------------------------------------------------------
# Bug #1 — saut du STT à la relance si transcript en cache
# ---------------------------------------------------------------------------

def test_run_summary_skips_stt_when_cached(app, owner_id, monkeypatch, tmp_path):
    with app.app_context():
        cfg = _cfg(tmp_path)
        job = JobStore.create_job(owner_id, "Cache")
        runner = WorkflowRunner(JobStore, cfg)
        fs = runner._get_fs(cfg, job.id)
        fs.save_text("summary/quick_transcript.txt", "transcript en cache")
        fs.save_json("summary/summary.json", {"segments": [{"text": "seg1"}, {"text": "seg2"}]})

        called = {"stt": False}
        monkeypatch.setattr(WorkflowRunner, "_run_quick_transcription",
                            lambda self, *a, **k: called.__setitem__("stt", True) or {})
        monkeypatch.setattr(WorkflowRunner, "_run_audio_scene_before_participants", lambda self, *a, **k: {})
        monkeypatch.setattr(WorkflowRunner, "_run_pyannote_after_transcription", lambda self, *a, **k: None)
        monkeypatch.setattr(WorkflowRunner, "_run_llm_summary", lambda self, job, result, config, sl: None)

        runner.run_summary(job, str(tmp_path / "a.wav"), cfg)
        assert called["stt"] is False                       # STT GPU non rappelé


# ---------------------------------------------------------------------------
# Bug #2 — _reclaim_vram_from_idle_arbitrage_llm
# ---------------------------------------------------------------------------

def test_reclaim_stops_idle_llm_when_lock_free(app, owner_id, monkeypatch, tmp_path):
    with app.app_context():
        runner = WorkflowRunner(JobStore, _cfg(tmp_path))
        stopped = {"n": 0}
        monkeypatch.setattr(runner.vram, "is_arbitrage_llm_running", lambda: True)
        monkeypatch.setattr(runner.allocator, "try_acquire_llm", lambda *a, **k: True)   # verrou libre
        monkeypatch.setattr(runner.allocator, "release_llm", lambda *a, **k: None)
        monkeypatch.setattr(runner.vram, "stop_arbitrage_llm", lambda: stopped.__setitem__("n", stopped["n"] + 1) or True)

        assert runner._reclaim_vram_from_idle_arbitrage_llm(_DummySL()) is True
        assert stopped["n"] == 1


def test_reclaim_does_not_stop_llm_in_use(app, owner_id, monkeypatch, tmp_path):
    with app.app_context():
        runner = WorkflowRunner(JobStore, _cfg(tmp_path))
        stopped = {"n": 0}
        monkeypatch.setattr(runner.vram, "is_arbitrage_llm_running", lambda: True)
        monkeypatch.setattr(runner.allocator, "try_acquire_llm", lambda *a, **k: False)  # verrou détenu
        monkeypatch.setattr(runner.vram, "stop_arbitrage_llm", lambda: stopped.__setitem__("n", stopped["n"] + 1) or True)

        assert runner._reclaim_vram_from_idle_arbitrage_llm(_DummySL()) is False
        assert stopped["n"] == 0


def test_run_transcription_reclaims_then_succeeds(app, owner_id, monkeypatch, tmp_path):
    with app.app_context():
        cfg = _cfg(tmp_path)
        job = JobStore.create_job(owner_id, "Reclaim")
        runner = WorkflowRunner(JobStore, cfg)

        reserves = {"n": 0}

        def fake_reserve(job, required_mb, phase):
            reserves["n"] += 1
            if reserves["n"] == 1:
                return None, False                          # 1er essai : VRAM bloquée
            return SimpleNamespace(gpu_index=0), False       # après reclaim : OK

        monkeypatch.setattr(runner, "_reserve_gpu_phase", fake_reserve)
        monkeypatch.setattr(runner, "_release_gpu_phase", lambda *a, **k: None)
        monkeypatch.setattr(WorkflowRunner, "_reclaim_vram_from_idle_arbitrage_llm", lambda self, sl: True)

        import transcria.stt.transcription as tr
        monkeypatch.setattr(tr, "Transcriber", lambda config, gpu_index=0: SimpleNamespace(
            transcribe=lambda job, path: {"segments": [{"text": "ok"}]}))

        result = runner.run_transcription(job, str(tmp_path / "a.wav"), cfg)
        assert reserves["n"] == 2                           # reclaim + 2e réservation
        assert not result.get("vram_wait")
        assert "error" not in result


# ---------------------------------------------------------------------------
# Split / nœud de ressources : STT du résumé distant ⇒ pas de réservation VRAM locale
# ---------------------------------------------------------------------------

def test_quick_transcription_skips_local_gpu_when_stt_remote(app, owner_id, monkeypatch, tmp_path):
    with app.app_context():
        cfg = _cfg(tmp_path)
        # Route le STT cohere vers un nœud distant (inference.mode=remote + endpoint).
        cfg["inference"] = {"mode": "remote", "stt": {"backends": {"cohere": {"url": "http://node:9000"}}}}
        job = JobStore.create_job(owner_id, "Remote STT")
        runner = WorkflowRunner(JobStore, cfg)

        # Le GPUSession local ne doit JAMAIS être ouvert quand le STT est distant.
        def _boom(*a, **k):
            raise AssertionError("_gpu_session ne doit pas être appelé en STT distant")

        monkeypatch.setattr(runner, "_gpu_session", _boom)
        from transcria.stt.summary import SummaryGenerator
        monkeypatch.setattr(SummaryGenerator, "generate_quick_summary",
                            lambda self, job, audio_path, gpu_index=0: {"transcript_text": "remote", "segment_count": 2})

        result = runner._run_quick_transcription(job, str(tmp_path / "a.wav"), cfg, _DummySL())
        assert result.get("segment_count") == 2
        assert not result.get("vram_wait")


def test_run_diarization_skips_local_gpu_when_remote(app, owner_id, monkeypatch, tmp_path):
    with app.app_context():
        cfg = _cfg(tmp_path)
        cfg["models"]["diarization_backend"] = "remote"   # _phase_runs_remotely("diarization") = True
        job = JobStore.create_job(owner_id, "Remote diar")
        runner = WorkflowRunner(JobStore, cfg)

        def _boom(*a, **k):
            raise AssertionError("_gpu_session ne doit pas être appelé en diarisation distante")

        monkeypatch.setattr(runner, "_gpu_session", _boom)
        monkeypatch.setattr(runner, "_cuda_available", lambda: True)  # même avec CUDA, le distant skippe
        monkeypatch.setattr(WorkflowRunner, "_inject_speaker_genders", lambda self, fs, scene: {})
        monkeypatch.setattr(
            "transcria.stt.diarizer_factory.create_diarizer",
            lambda config, device=None, progress_callback=None: SimpleNamespace(
                diarize=lambda job, path: {"available": True, "speakers": [{"speaker_id": "SPEAKER_00"}]},
                offload=lambda: None,
            ),
        )

        result = runner.run_diarization(job, str(tmp_path / "a.wav"), cfg)
        assert result.get("available") is True
        assert not result.get("vram_wait")


class _DummySL:
    """Logger structuré factice : absorbe info/warning/error/exception/debug + set_context."""
    def __getattr__(self, _name):
        return lambda *a, **k: None


# ---------------------------------------------------------------------------
# Chasse aux bugs (batch E2E 2026-07-05) — correction : retry sur GEL opencode
# ---------------------------------------------------------------------------

def _corr_setup(app, owner_id, cfg, monkeypatch):
    from transcria.jobs.filesystem import JobFilesystem
    job = JobStore.create_job(owner_id, "Corr")
    runner = WorkflowRunner(JobStore, cfg)
    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    fs.save_text("metadata/transcription.srt", "1\n00:00:00,000 --> 00:00:03,000\nBonjour.\n")
    fs.save_text("context/job_context.yaml", "{}")
    fs.save_json("context/session_lexicon.json", [])
    monkeypatch.setattr(runner.allocator, "try_acquire_llm", lambda *a, **k: True)
    monkeypatch.setattr(runner.allocator, "release_llm", lambda *a, **k: None)
    monkeypatch.setattr(runner.vram, "is_arbitrage_llm_running", lambda: True)
    monkeypatch.setattr(runner.vram, "ensure_arbitrage_llm_ready", lambda **k: True)
    monkeypatch.setattr(WorkflowRunner, "_materialize_meeting_invite", staticmethod(lambda fs, job: None))
    monkeypatch.setattr(WorkflowRunner, "_corrected_srt_integrity_error", staticmethod(lambda src, corr: None))
    return job, runner, fs


def test_correction_retries_on_opencode_hang(app, owner_id, monkeypatch, tmp_path):
    # Un GEL opencode (watchdog → success=False, « opencode interrompu … ») est TRANSITOIRE
    # (deadlock de démarrage intermittent) → la correction RETENTE au lieu d'échouer au 1er
    # coup. Avant : `if not result["success"] ... break` coupait la boucle sur tout échec.
    with app.app_context():
        job, runner, fs = _corr_setup(app, owner_id, _cfg(tmp_path), monkeypatch)
        calls = {"n": 0}
        good = "1\n00:00:00,000 --> 00:00:03,000\nBonjour corrige.\n"

        def fake_run_correction(self, srt, ctx, lex, invite=None, **_kw):
            calls["n"] += 1
            if calls["n"] < 3:
                return {"success": False, "corrected_srt": "", "report": "",
                        "error": "opencode interrompu (gel au démarrage opencode (pré-session))"}
            return {"success": True, "corrected_srt": good, "report": "ok", "error": ""}
        monkeypatch.setattr(OpenCodeRunner, "run_correction", fake_run_correction)

        result = runner.run_correction(job, _cfg(tmp_path))
        assert calls["n"] == 3
        assert result["success"] is True
        assert fs.load_text("metadata/transcription_corrigee.srt")


def test_correction_hard_failure_does_not_retry(app, owner_id, monkeypatch, tmp_path):
    # Un échec DUR (success=False SANS « interrompu ») n'est pas transitoire → pas de retry.
    with app.app_context():
        job, runner, fs = _corr_setup(app, owner_id, _cfg(tmp_path), monkeypatch)
        calls = {"n": 0}

        def fake_run_correction(self, srt, ctx, lex, invite=None, **_kw):
            calls["n"] += 1
            return {"success": False, "corrected_srt": "", "report": "", "error": "erreur dure quelconque"}
        monkeypatch.setattr(OpenCodeRunner, "run_correction", fake_run_correction)

        result = runner.run_correction(job, _cfg(tmp_path))
        assert calls["n"] == 1
        assert result["success"] is False

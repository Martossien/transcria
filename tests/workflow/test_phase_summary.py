"""Tests de la phase RÉSUMÉ (workflow/phases/summary*.py) — migrés de test_workflow_runner.py (B1 lot 2).

Les tests traversent la façade ``WorkflowRunner`` : les coutures historiques
(``runner._gpu_session``, ``runner.vram.*``, ``runner._should_reserve_llm_vram``…)
restent les points de substitution.
"""
import pytest  # noqa: F401 — parité d'environnement avec test_workflow_runner.py

from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job, JobState  # noqa: F401
from transcria.jobs.store import JobStore
from transcria.workflow.runner import WorkflowRunner


def _default_config(**overrides):
    cfg = {
        "storage": {"jobs_dir": "/tmp/test_transcria_jobs"},
        "workflow": {
            "enable_quick_summary": True,
            "enable_speaker_detection": True,
            "enable_quality_mode": True,
            "summary_llm": {"enabled": False},
            "arbitration_llm": {"model_id": "local/test-llm-arbitrage"},
        },
        "services": {
            "arbitrage_script": "/bin/true",
            "stop_script": "/bin/true",
            "arbitrage_llm_port": 8080,
            "vllm_port": 8000,
        },
        "models": {"cohere_model_path": "/tmp/fake_model"},
    }
    for k, v in overrides.items():
        cfg[k] = v
    return cfg


class TestWorkflowRunnerRunSummaryOpencodeConfig:
    def test_run_summary_uses_summary_llm_model_id(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(
                storage={"jobs_dir": str(tmp_path / "jobs")},
                workflow={
                    "enable_quick_summary": True,
                    "enable_speaker_detection": True,
                    "enable_quality_mode": True,
                    "summary_llm": {
                        "enabled": True,
                        "model_id": "local/summary-model-test",
                        "timeout_seconds": 4321,
                    },
                    "arbitration_llm": {
                        "model_id": "local/test-llm-arbitrage",
                        "timeout_seconds": 1234,
                        "opencode_bin": "opencode",
                    },
                },
            )
            job = JobStore.create_job(owner_id, "Summary Model Config")
            runner = WorkflowRunner(JobStore, cfg)

            monkeypatch.setattr(runner.vram, "free_all_gpus", lambda: True)
            monkeypatch.setattr(runner.vram, "launch_arbitrage_llm", lambda: True)
            monkeypatch.setattr(runner.vram, "stop_arbitrage_llm", lambda: True)
            monkeypatch.setattr(runner.vram, "is_arbitrage_llm_running", lambda: True)
            monkeypatch.setattr(runner.vram, "ensure_arbitrage_llm_ready", lambda expected_model_id=None: True)

            from transcria.gpu.opencode_runner import OpenCodeRunner
            from transcria.jobs.filesystem import JobFilesystem

            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
            fs.save_text("summary/quick_transcript.txt", "Bonjour")

            result = {"transcript_text": "Bonjour", "transcript_short": "Bonjour"}
            captured = {}

            def fake_run_summary(self, transcript_path, context_path=None, diarization_context_path=None, invite_path=None, **kwargs):
                captured["model_ref"] = self.model_ref
                captured["summary_timeout"] = self._get_summary_timeout()
                return {"summary_text": "Résumé", "title_suggere": "Titre"}

            monkeypatch.setattr(OpenCodeRunner, "run_summary", fake_run_summary)

            runner._run_llm_summary(job, result, cfg, type("SL", (), {"info": lambda *a, **k: None})())

            assert captured["model_ref"] == "local/summary-model-test"
            assert captured["summary_timeout"] == 4321


class TestWorkflowRunnerRunSummary:
    def test_run_summary_vram_insufficient(self, app, owner_id, monkeypatch, tmp_path):
        """_gpu_session lève GPUSessionError → signal `vram_wait` (PAS FAILED).

        Une VRAM insuffisante est transitoire : run_summary remonte `vram_wait` et
        restaure l'état pré-résumé au lieu de marquer FAILED. L'appelant (api_summary)
        met alors le job en attente et le client relance automatiquement.
        """
        import contextlib

        from transcria.gpu.gpu_session import GPUSessionError

        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "VRAM Fail")
            prior_state = job.state
            runner = WorkflowRunner(JobStore, cfg)

            @contextlib.contextmanager
            def fake_gpu_session(job, model_name, required_mb, phase):
                raise GPUSessionError("VRAM insuffisante (simulé)")
                yield  # noqa: unreachable

            monkeypatch.setattr(runner, "_gpu_session", fake_gpu_session)

            result = runner.run_summary(job, "/tmp/fake.wav", cfg)
            assert result.get("vram_wait") is True
            assert result.get("required_mb")
            assert result.get("phase") == "summary_stt"

            updated = JobStore.get_by_id(job.id)
            # Le job N'EST PAS FAILED : il est revenu à son état pré-résumé.
            assert updated.state != JobState.FAILED.value
            assert updated.state == prior_state

    def test_run_summary_success_cohere_only(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(
                storage={"jobs_dir": str(tmp_path / "jobs")},
                workflow={"enable_quick_summary": True, "enable_speaker_detection": True, "enable_quality_mode": True, "summary_llm": {"enabled": False}},
            )
            job = JobStore.create_job(owner_id, "Summary OK")
            runner = WorkflowRunner(JobStore, cfg)

            from transcria.jobs.filesystem import JobFilesystem as _JFS

            fs = _JFS(cfg["storage"]["jobs_dir"], job.id)

            monkeypatch.setattr(runner.vram, "ensure_free", lambda required_mb: 0)
            monkeypatch.setattr(runner.vram, "untrack_model", lambda name: None)
            monkeypatch.setattr(runner.vram, "offload_all", lambda: None)

            from transcria.stt.summary import SummaryGenerator

            fake_summary_result = {
                "transcript_text": "[0s->5s] Bonjour à tous",
                "transcript_short": "Bonjour à tous",
                "summary_text": "Résumé de contrôle indisponible (LLM non configurée).",
                "segment_count": 1,
            }
            monkeypatch.setattr(SummaryGenerator, "generate_quick_summary", lambda *a, **kw: fake_summary_result)

            audio_path = str(tmp_path / "test.wav")
            with open(audio_path, "w") as f:
                f.write("fake")

            result = runner.run_summary(job, audio_path, cfg)
            assert result["segment_count"] == 1
            assert result["transcript_text"] == "[0s->5s] Bonjour à tous"

            updated = JobStore.get_by_id(job.id)
            assert updated.state == JobState.SUMMARY_DONE.value

    def test_run_summary_uses_effective_backend_for_gpu_session(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(
                storage={"jobs_dir": str(tmp_path / "jobs")},
                models={"stt_backend": "granite", "cohere_model_path": "/tmp/fake_model"},
                gpu={"cohere_vram_mb": 6000, "granite_vram_mb": 7200},
                granite={"model_id": "./models/granite-speech-4.1-2b"},
                workflow={"enable_quick_summary": True, "enable_speaker_detection": False, "enable_quality_mode": True, "summary_llm": {"enabled": False}},
            )
            job = JobStore.create_job(owner_id, "Summary Granite")
            runner = WorkflowRunner(JobStore, cfg)
            captured = {}

            class FakeSession:
                def __init__(self, vram, model_name, required_mb):
                    captured["model_name"] = model_name
                    captured["required_mb"] = required_mb
                    self.gpu_index = 4

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

            from transcria.stt.summary import SummaryGenerator
            from transcria.workflow import gpu_phase as gpu_phase_module

            # B1 : la session GPU vit dans workflow/gpu_phase.py — substitution à la source.
            monkeypatch.setattr(gpu_phase_module, "GPUSession", FakeSession)
            monkeypatch.setattr(
                SummaryGenerator,
                "generate_quick_summary",
                lambda *a, **kw: {
                    "transcript_text": "[0s->1s] Bonjour",
                    "transcript_short": "Bonjour",
                    "summary_text": "Résumé de contrôle indisponible (LLM non configurée).",
                    "segment_count": 1,
                },
            )

            result = runner.run_summary(job, str(tmp_path / "test.wav"), cfg)

            assert result["segment_count"] == 1
            assert captured == {"model_name": "granite-summary", "required_mb": 7200}

    def test_run_summary_success_with_llm(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(
                storage={"jobs_dir": str(tmp_path / "jobs")},
                workflow={"enable_quick_summary": True, "enable_speaker_detection": True, "enable_quality_mode": True, "summary_llm": {"enabled": True}, "arbitration_llm": {"model_id": "local/test-llm-arbitrage"}},
            )
            job = JobStore.create_job(owner_id, "Summary LLM OK")
            runner = WorkflowRunner(JobStore, cfg)


            monkeypatch.setattr(runner.vram, "ensure_free", lambda required_mb: 0)
            monkeypatch.setattr(runner.vram, "untrack_model", lambda name: None)
            monkeypatch.setattr(runner.vram, "offload_all", lambda: None)
            monkeypatch.setattr(runner.vram, "free_all_gpus", lambda: True)
            monkeypatch.setattr(runner.vram, "launch_arbitrage_llm", lambda: True)
            monkeypatch.setattr(runner.vram, "stop_arbitrage_llm", lambda: True)
            monkeypatch.setattr(runner.vram, "is_arbitrage_llm_running", lambda: True)
            monkeypatch.setattr(runner.vram, "ensure_arbitrage_llm_ready", lambda expected_model_id=None: True)

            from transcria.gpu.opencode_runner import OpenCodeRunner
            from transcria.stt.speaker_detection import SpeakerDetector
            from transcria.stt.summary import SummaryGenerator

            fake_summary_result = {
                "transcript_text": "[0s->60s] Discussion sur le budget",
                "transcript_short": "Discussion sur le budget",
                "summary_text": "Résumé de contrôle indisponible (LLM non configurée).",
                "segment_count": 5,
            }
            monkeypatch.setattr(SummaryGenerator, "generate_quick_summary", lambda *a, **kw: fake_summary_result)

            fake_speakers_result = {
                "available": True,
                "speakers": [
                    {"speaker_id": "SPEAKER_00", "speaking_time_seconds": 60, "turn_count": 5},
                ],
            }
            monkeypatch.setattr(SpeakerDetector, "detect", lambda self, job, audio_path, device="cpu": fake_speakers_result)
            import torch
            monkeypatch.setattr(torch.cuda, "is_available", lambda: False)

            from transcria.jobs.filesystem import JobFilesystem

            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)

            def fake_run_summary(self_runner, transcript_path, context_path=None, diarization_context_path=None, invite_path=None, **kwargs):
                fs_dir = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
                fs_dir.save_text("summary/summary.md", "# Résumé\n\n**Titre suggéré :** Budget Q1\n")
                return {
                    "_summary_produced": True,
                    "summary_text": "# Résumé\n\n**Titre suggéré :** Budget Q1\n",
                    "title_suggere": "Budget Q1",
                    "type_suggere": "Réunion interne",
                    "speaker_count": 0,
                }

            monkeypatch.setattr(OpenCodeRunner, "run_summary", fake_run_summary)

            audio_path = str(tmp_path / "test.wav")
            with open(audio_path, "w") as f:
                f.write("fake")

            result = runner.run_summary(job, audio_path, cfg)
            assert result["segment_count"] == 5

            updated = JobStore.get_by_id(job.id)
            assert updated.state == JobState.SUMMARY_DONE.value

            meeting_ctx = fs.load_json("context/meeting_context.json")
            assert meeting_ctx is not None
            assert meeting_ctx.get("title_suggere") == "Budget Q1"

    def _llm_phase_runner(self, cfg, job, monkeypatch, tmp_path):
        """Mocks communs pour atteindre la sous-étape LLM du résumé sans GPU réel."""
        runner = WorkflowRunner(JobStore, cfg)
        import torch

        from transcria.stt.speaker_detection import SpeakerDetector
        from transcria.stt.summary import SummaryGenerator

        monkeypatch.setattr(runner.vram, "ensure_free", lambda required_mb: 0)
        monkeypatch.setattr(runner.vram, "offload_all", lambda: None)
        monkeypatch.setattr(runner.vram, "stop_arbitrage_llm", lambda: True)
        monkeypatch.setattr(SummaryGenerator, "generate_quick_summary", lambda *a, **kw: {
            "transcript_text": "[0s->60s] Discussion", "transcript_short": "Discussion",
            "summary_text": "Résumé de contrôle indisponible (LLM non configurée).",
            "segment_count": 3,
        })
        monkeypatch.setattr(SpeakerDetector, "detect",
                            lambda self, job, audio_path, device="cpu": {"available": False})
        monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
        return runner

    def test_run_summary_llm_vram_shortage_returns_vram_wait(self, app, owner_id, monkeypatch, tmp_path):
        """Doctrine « VRAM insuffisante = attente, jamais dégradé silencieux » : la pénurie
        VRAM de la sous-étape LLM remontait un skip muet → SUMMARY_DONE avec placeholder
        (incident Ministral du 11/06/2026). Désormais : vram_wait + état restauré."""
        with app.app_context():
            cfg = _default_config(
                storage={"jobs_dir": str(tmp_path / "jobs")},
                workflow={"enable_quick_summary": True, "summary_llm": {"enabled": True},
                          "arbitration_llm": {"model_id": "local/test-llm"}},
            )
            job = JobStore.create_job(owner_id, "Summary LLM VRAM")
            prior_state = job.state
            runner = self._llm_phase_runner(cfg, job, monkeypatch, tmp_path)
            monkeypatch.setattr(runner.allocator, "try_acquire_llm", lambda job_id, timeout_s=300: True)
            monkeypatch.setattr(runner.allocator, "release_llm", lambda job_id: None)
            monkeypatch.setattr(runner, "_should_reserve_llm_vram", lambda: True)
            monkeypatch.setattr(runner.vram, "is_arbitrage_llm_running", lambda: False)
            # Pénurie pour la réservation MULTI-GPU de la LLM (le STT rapide passe).
            monkeypatch.setattr(
                runner.allocator, "try_reserve_llm",
                lambda job_id, total_mb, phase: False,
            )

            result = runner.run_summary(job, "/tmp/fake.wav", cfg)

            assert result.get("vram_wait") is True
            assert result.get("phase") == "summary_llm"
            updated = JobStore.get_by_id(job.id)
            assert updated.state == prior_state  # restauré, PAS summary_done
            assert not (updated.get_extra_data() or {}).get("summary_llm_failed")

    def test_run_summary_llm_lock_busy_returns_vram_wait(self, app, owner_id, monkeypatch, tmp_path):
        """Verrou LLM occupé par un autre job : attente (avant : skip muet → SUMMARY_DONE)."""
        with app.app_context():
            cfg = _default_config(
                storage={"jobs_dir": str(tmp_path / "jobs")},
                workflow={"enable_quick_summary": True, "summary_llm": {"enabled": True},
                          "arbitration_llm": {"model_id": "local/test-llm"}},
            )
            job = JobStore.create_job(owner_id, "Summary LLM verrou")
            prior_state = job.state
            runner = self._llm_phase_runner(cfg, job, monkeypatch, tmp_path)
            monkeypatch.setattr(runner.allocator, "try_acquire_llm", lambda job_id, timeout_s=300: False)

            result = runner.run_summary(job, "/tmp/fake.wav", cfg)

            assert result.get("vram_wait") is True
            assert "verrou" in (result.get("reason") or "")
            assert JobStore.get_by_id(job.id).state == prior_state

    def test_run_summary_llm_launch_failure_flags_relaunchable(self, app, owner_id, monkeypatch, tmp_path):
        """Panne de lancement LLM : signalée + relançable (avant : SUMMARY_DONE placeholder)."""
        with app.app_context():
            cfg = _default_config(
                storage={"jobs_dir": str(tmp_path / "jobs")},
                workflow={"enable_quick_summary": True, "summary_llm": {"enabled": True},
                          "arbitration_llm": {"model_id": "local/test-llm"}},
            )
            job = JobStore.create_job(owner_id, "Summary LLM panne")
            prior_state = job.state
            runner = self._llm_phase_runner(cfg, job, monkeypatch, tmp_path)
            monkeypatch.setattr(runner.allocator, "try_acquire_llm", lambda job_id, timeout_s=300: True)
            monkeypatch.setattr(runner.allocator, "release_llm", lambda job_id: None)
            monkeypatch.setattr(runner, "_should_reserve_llm_vram", lambda: False)
            monkeypatch.setattr(runner.vram, "is_arbitrage_llm_running", lambda: False)
            monkeypatch.setattr(runner.vram, "ensure_arbitrage_llm_ready",
                                lambda expected_model_id=None: False)

            result = runner.run_summary(job, "/tmp/fake.wav", cfg)

            assert result.get("summary_llm_failed") is True
            updated = JobStore.get_by_id(job.id)
            assert updated.state == prior_state  # pas SUMMARY_DONE
            assert (updated.get_extra_data() or {}).get("summary_llm_failed")

    def test_run_summary_exception_sets_failed(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Summary Crash")
            runner = WorkflowRunner(JobStore, cfg)

            monkeypatch.setattr(runner.vram, "ensure_free", lambda required_mb: 0)
            monkeypatch.setattr(runner.vram, "offload_all", lambda: None)
            monkeypatch.setattr(runner.vram, "stop_arbitrage_llm", lambda: True)

            from transcria.stt.summary import SummaryGenerator

            monkeypatch.setattr(SummaryGenerator, "generate_quick_summary", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("GPU crash")))

            result = runner.run_summary(job, "/tmp/fake.wav", cfg)
            assert result["error"] == "GPU crash"
            assert "indisponible" in result["summary_text"]

            updated = JobStore.get_by_id(job.id)
            assert updated.state == JobState.FAILED.value
            assert updated.error_message == "GPU crash"

    def test_run_summary_stt_error_dict_sets_failed(self, app, owner_id, monkeypatch, tmp_path):
        """STT renvoyant un dict d'erreur (sans exception) ne doit pas laisser SUMMARY_RUNNING orphelin."""
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Summary STT Error Dict")
            runner = WorkflowRunner(JobStore, cfg)

            monkeypatch.setattr(runner.vram, "ensure_free", lambda required_mb: 0)
            monkeypatch.setattr(runner.vram, "offload_all", lambda: None)

            from transcria.stt.summary import SummaryGenerator

            monkeypatch.setattr(
                SummaryGenerator, "generate_quick_summary",
                lambda *a, **kw: {"error": "backend HS", "transcript_text": "", "summary_text": "Résumé indisponible."},
            )

            result = runner.run_summary(job, "/tmp/fake.wav", cfg)
            assert result["error"] == "backend HS"

            updated = JobStore.get_by_id(job.id)
            assert updated.state == JobState.FAILED.value
            assert updated.error_message == "backend HS"

    def test_run_summary_with_speaker_detection_enabled(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(
                storage={"jobs_dir": str(tmp_path / "jobs")},
                workflow={"enable_quick_summary": True, "enable_speaker_detection": True, "enable_quality_mode": True, "summary_llm": {"enabled": False}},
            )
            job = JobStore.create_job(owner_id, "Summary + Speakers")
            runner = WorkflowRunner(JobStore, cfg)

            from transcria.jobs.filesystem import JobFilesystem as _JFS

            monkeypatch.setattr(runner.vram, "ensure_free", lambda required_mb: 0)
            monkeypatch.setattr(runner.vram, "untrack_model", lambda name: None)
            monkeypatch.setattr(runner.vram, "offload_all", lambda: None)

            from transcria.stt.speaker_detection import SpeakerDetector
            from transcria.stt.summary import SummaryGenerator

            fake_summary_result = {
                "transcript_text": "[0s->5s] Bonjour",
                "transcript_short": "Bonjour",
                "summary_text": "Résumé indisponible.",
                "segment_count": 1,
            }
            monkeypatch.setattr(SummaryGenerator, "generate_quick_summary", lambda *a, **kw: fake_summary_result)

            fake_speakers_result = {
                "available": True,
                "speakers": [
                    {"speaker_id": "SPEAKER_00", "speaking_time_seconds": 60, "turn_count": 5},
                    {"speaker_id": "SPEAKER_01", "speaking_time_seconds": 30, "turn_count": 3},
                ],
            }
            monkeypatch.setattr(SpeakerDetector, "detect", lambda self, job, audio_path, device="cpu": fake_speakers_result)

            audio_path = str(tmp_path / "test.wav")
            with open(audio_path, "w") as f:
                f.write("fake")

            result = runner.run_summary(job, audio_path, cfg)
            assert result["segment_count"] == 1

            updated = JobStore.get_by_id(job.id)
            assert updated.state == JobState.SUMMARY_DONE.value

            fs = _JFS(cfg["storage"]["jobs_dir"], job.id)
            ctx = fs.load_json("context/meeting_context.json")
            assert ctx is not None
            assert ctx.get("speaker_count_pyannote") == 2

    def test_audio_scene_runs_before_participants_when_enabled(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(
                storage={"jobs_dir": str(tmp_path / "jobs")},
                workflow={
                    "audio_scene": {"enabled": True, "detect_gender": True},
                    "audio_quality": {},
                },
            )
            job = JobStore.create_job(owner_id, "Summary Scene")
            runner = WorkflowRunner(JobStore, cfg)

            from transcria.audio.scene_analyzer import AudioSceneAnalyzer

            scene = {
                "gender": {"has_gender_data": True, "dominant": "female"},
                "gender_segments": [{"start": 0.0, "end": 2.0, "label": "female"}],
                "speech_ratio": 0.8,
            }
            monkeypatch.setattr(AudioSceneAnalyzer, "analyze", lambda self, path: scene)

            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
            fs.save_json("metadata/audio_analysis.json", {"duration_seconds": 10})
            audio_path = str(tmp_path / "test.wav")
            with open(audio_path, "w") as f:
                f.write("fake")

            result = runner._run_audio_scene_before_participants(
                job,
                audio_path,
                cfg,
                type("Log", (), {"debug": lambda *a, **k: None, "info": lambda *a, **k: None, "warning": lambda *a, **k: None})(),
            )

            assert result["gender"]["has_gender_data"] is True
            assert fs.load_json("metadata/audio_scene.json")["gender_segments"][0]["label"] == "female"
            assert fs.load_json("metadata/audio_quality_decision.json")["level"] == "ok"

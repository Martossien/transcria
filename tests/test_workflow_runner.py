"""Tests for WorkflowRunner — success paths for all steps, VRAM cycle, GPU allocation, state transitions."""
import json
import pytest

from transcria.workflow.runner import WorkflowRunner
from transcria.workflow.states import WorkflowState, StepStatus
from transcria.jobs.models import Job, JobState
from transcria.jobs.store import JobStore
from transcria.gpu.vram_manager import VRAMManager
from transcria.jobs.filesystem import JobFilesystem


def _default_config(**overrides):
    cfg = {
        "storage": {"jobs_dir": "/tmp/test_transcria_jobs"},
        "workflow": {
            "enable_quick_summary": True,
            "enable_speaker_detection": True,
            "enable_quality_mode": True,
            "summary_llm": {"enabled": False},
        },
        "services": {
            "dashboard_llm_url": "http://127.0.0.1:5001",
            "arbitrage_script": "/bin/true",
            "stop_script": "/bin/true",
            "qwen_port": 8080,
            "vllm_port": 8000,
        },
        "models": {"cohere_model_path": "/tmp/fake_model"},
    }
    for k, v in overrides.items():
        cfg[k] = v
    return cfg


class TestWorkflowRunnerRunAnalyze:
    def test_run_analyze_success(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Analyze Test")
            runner = WorkflowRunner(JobStore, cfg)

            from transcria.audio.analyzer import AudioAnalyzer

            fake_result = {
                "duration_seconds": 120,
                "format": "mp3",
                "codec": "mp3",
                "channels": 1,
                "sample_rate_hz": 16000,
                "size_bytes": 1024,
            }

            monkeypatch.setattr(AudioAnalyzer, "analyze", lambda path: fake_result)

            audio_path = str(tmp_path / "test_audio.mp3")
            with open(audio_path, "w") as f:
                f.write("fake audio")

            result = runner.run_analyze(job, audio_path)
            assert result["format"] == "mp3"
            assert result["duration_seconds"] == 120

            updated = JobStore.get_by_id(job.id)
            assert updated.state == JobState.ANALYZED.value

    def test_run_analyze_ffprobe_failure(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Analyze Fail")
            runner = WorkflowRunner(JobStore, cfg)

            from transcria.audio.analyzer import AudioAnalyzer

            def raise_error(path):
                raise FileNotFoundError(f"Fichier introuvable: {path}")

            monkeypatch.setattr(AudioAnalyzer, "analyze", raise_error)

            with pytest.raises(FileNotFoundError):
                runner.run_analyze(job, "/nonexistent/audio.mp3")


class TestWorkflowRunnerSpeakerRoles:
    def test_apply_speaker_roles_splits_legacy_label_in_role(self, tmp_path):
        fs = JobFilesystem(str(tmp_path), "job-speaker-roles")
        fs.save_json("speakers/speaker_mapping.json", {
            "mapping": {
                "SPEAKER_00": {"name": "Fonction A", "participant_id": "p1"},
            }
        })
        fs.save_json("context/participants.json", [
            {"id": "p1", "name": "Fonction A", "function": "", "service": "", "role": ""},
        ])

        class Log:
            @staticmethod
            def info(*args, **kwargs):
                pass

        WorkflowRunner._apply_speaker_roles(
            fs,
            {"SPEAKER_00": {"label": "", "role": "Fonction A — décrit une action observée"}},
            Log(),
        )

        participants = fs.load_json("context/participants.json")
        assert participants[0]["name"] == "Fonction A"
        assert participants[0]["role"] == "décrit une action observée"


class TestWorkflowRunnerRunCorrection:
    def test_run_correction_passes_config_and_keeps_partial_timeout_output(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(
                storage={"jobs_dir": str(tmp_path / "jobs")},
                workflow={
                    "enable_quick_summary": True,
                    "enable_speaker_detection": True,
                    "enable_quality_mode": True,
                    "summary_llm": {"enabled": False},
                    "arbitration_llm": {"timeout_seconds": 1234, "opencode_bin": "opencode"},
                },
            )
            job = JobStore.create_job(owner_id, "Correction Partial Timeout")
            runner = WorkflowRunner(JobStore, cfg)

            from transcria.jobs.filesystem import JobFilesystem
            from transcria.gpu.opencode_runner import OpenCodeRunner

            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
            fs.save_text("metadata/transcription.srt", "1\n00:00:00,000 --> 00:00:05,000\nBonjour\n")
            fs.save_text("context/job_context.yaml", "meeting: {}\n")
            fs.save_text("context/session_lexicon.json", "[]\n")

            monkeypatch.setattr(runner.vram, "free_all_gpus", lambda: True)
            monkeypatch.setattr(runner.vram, "launch_qwen_35b", lambda: True)
            monkeypatch.setattr(runner.vram, "stop_qwen_35b", lambda: True)

            captured = {}

            def fake_run_correction(self, srt_path, context_path, lexicon_path):
                captured["config_timeout"] = self._get_correction_timeout()
                return {
                    "success": True,
                    "corrected_srt": "1\n00:00:00,000 --> 00:00:05,000\nBonjour corrigé\n",
                    "report": "# Rapport\n",
                    "warning": "opencode timeout après 1234s",
                    "error": "",
                }

            monkeypatch.setattr(OpenCodeRunner, "run_correction", fake_run_correction)

            result = runner.run_correction(job, cfg)

            assert result["success"] is True
            assert captured["config_timeout"] == 1234
            assert "corrigé" in fs.load_text("metadata/transcription_corrigee.srt")


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
                        "timeout_seconds": 1234,
                        "opencode_bin": "opencode",
                    },
                },
            )
            job = JobStore.create_job(owner_id, "Summary Model Config")
            runner = WorkflowRunner(JobStore, cfg)

            monkeypatch.setattr(runner.vram, "free_all_gpus", lambda: True)
            monkeypatch.setattr(runner.vram, "launch_qwen_35b", lambda: True)
            monkeypatch.setattr(runner.vram, "stop_qwen_35b", lambda: True)

            from transcria.jobs.filesystem import JobFilesystem
            from transcria.gpu.opencode_runner import OpenCodeRunner

            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
            fs.save_text("summary/quick_transcript.txt", "Bonjour")

            result = {"transcript_text": "Bonjour", "transcript_short": "Bonjour"}
            captured = {}

            def fake_run_summary(self, transcript_path, context_path=None, diarization_context_path=None):
                captured["model_ref"] = self.model_ref
                captured["summary_timeout"] = self._get_summary_timeout()
                return {"summary_text": "Résumé", "title_suggere": "Titre"}

            monkeypatch.setattr(OpenCodeRunner, "run_summary", fake_run_summary)

            runner._run_llm_summary(job, result, cfg, type("SL", (), {"info": lambda *a, **k: None})())

            assert captured["model_ref"] == "local/summary-model-test"
            assert captured["summary_timeout"] == 4321


class TestPipelineServiceStateRecovery:
    def test_pipeline_marks_job_failed_when_step_returns_error(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            from transcria.services.pipeline_service import PipelineService

            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Pipeline Failure State")
            service = PipelineService(cfg)

            monkeypatch.setattr(
                service.runner,
                "run_transcription",
                lambda job_obj, audio_path, config: {"segments": []},
            )
            monkeypatch.setattr(
                service.runner,
                "run_correction",
                lambda job_obj, config: {"error": "qwen down"},
            )

            result = service.run_process(job, "/tmp/fake.wav", "fast")
            updated = JobStore.get_by_id(job.id)

            assert result["error"] == "qwen down"
            assert updated.state == JobState.FAILED.value


class TestWorkflowRunnerRunSummary:
    def test_run_summary_vram_insufficient(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "VRAM Fail")
            runner = WorkflowRunner(JobStore, cfg)

            monkeypatch.setattr(runner.vram, "ensure_free", lambda required_mb: None)

            result = runner.run_summary(job, "/tmp/fake.wav", cfg)
            assert "error" in result
            assert "VRAM" in result["error"]

            updated = JobStore.get_by_id(job.id)
            assert updated.state == JobState.FAILED.value

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

    def test_run_summary_success_with_llm(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(
                storage={"jobs_dir": str(tmp_path / "jobs")},
                workflow={"enable_quick_summary": True, "enable_speaker_detection": True, "enable_quality_mode": True, "summary_llm": {"enabled": True}},
            )
            job = JobStore.create_job(owner_id, "Summary LLM OK")
            runner = WorkflowRunner(JobStore, cfg)

            from transcria.jobs.filesystem import JobFilesystem as _JFS

            monkeypatch.setattr(runner.vram, "ensure_free", lambda required_mb: 0)
            monkeypatch.setattr(runner.vram, "untrack_model", lambda name: None)
            monkeypatch.setattr(runner.vram, "offload_all", lambda: None)
            monkeypatch.setattr(runner.vram, "free_all_gpus", lambda: True)
            monkeypatch.setattr(runner.vram, "launch_qwen_35b", lambda: True)
            monkeypatch.setattr(runner.vram, "stop_qwen_35b", lambda: True)

            from transcria.stt.summary import SummaryGenerator
            from transcria.gpu.opencode_runner import OpenCodeRunner
            from transcria.stt.speaker_detection import SpeakerDetector

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

            def fake_run_summary(self_runner, transcript_path, context_path=None, diarization_context_path=None):
                fs_dir = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
                fs_dir.save_text("summary/summary.md", "# Résumé\n\n**Titre suggéré :** Budget Q1\n")
                return {
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

    def test_run_summary_exception_sets_failed(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Summary Crash")
            runner = WorkflowRunner(JobStore, cfg)

            monkeypatch.setattr(runner.vram, "ensure_free", lambda required_mb: 0)
            monkeypatch.setattr(runner.vram, "offload_all", lambda: None)
            monkeypatch.setattr(runner.vram, "stop_qwen_35b", lambda: True)

            from transcria.stt.summary import SummaryGenerator

            monkeypatch.setattr(SummaryGenerator, "generate_quick_summary", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("GPU crash")))

            result = runner.run_summary(job, "/tmp/fake.wav", cfg)
            assert result["error"] == "GPU crash"
            assert "indisponible" in result["summary_text"]

            updated = JobStore.get_by_id(job.id)
            assert updated.state == JobState.FAILED.value
            assert updated.error_message == "GPU crash"

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

            from transcria.stt.summary import SummaryGenerator
            from transcria.stt.speaker_detection import SpeakerDetector

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


class TestWorkflowRunnerRunSpeakerDetection:
    def test_run_speaker_detection_success(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Speaker Detect")
            runner = WorkflowRunner(JobStore, cfg)

            from transcria.stt.speaker_detection import SpeakerDetector

            fake_result = {
                "available": True,
                "speakers": [
                    {"speaker_id": "SPEAKER_00", "speaking_time_seconds": 45, "turn_count": 4},
                ],
                "turns": [{"start": 0, "end": 5, "speaker": "SPEAKER_00"}],
            }
            monkeypatch.setattr(SpeakerDetector, "detect", lambda self, job, audio_path, device="cpu": fake_result)
            import torch
            monkeypatch.setattr(torch.cuda, "is_available", lambda: False)

            audio_path = str(tmp_path / "test.wav")
            with open(audio_path, "w") as f:
                f.write("fake")

            result = runner.run_speaker_detection(job, audio_path, cfg)
            assert result["available"] is True
            assert len(result["speakers"]) == 1

            updated = JobStore.get_by_id(job.id)
            assert updated.state == JobState.SPEAKER_DETECTION_DONE.value

    def test_run_speaker_detection_failure(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Speaker Fail")
            runner = WorkflowRunner(JobStore, cfg)

            from transcria.stt.speaker_detection import SpeakerDetector

            monkeypatch.setattr(SpeakerDetector, "detect", lambda self, job, audio_path, device="cpu": (_ for _ in ()).throw(RuntimeError("No GPU")))
            import torch
            monkeypatch.setattr(torch.cuda, "is_available", lambda: False)

            audio_path = str(tmp_path / "test.wav")
            with open(audio_path, "w") as f:
                f.write("fake")

            result = runner.run_speaker_detection(job, audio_path, cfg)
            assert "error" in result

            updated = JobStore.get_by_id(job.id)
            assert updated.state == JobState.FAILED.value


class TestWorkflowRunnerRunTranscription:
    def test_run_transcription_vram_insufficient(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Transcript VRAM Fail")
            runner = WorkflowRunner(JobStore, cfg)

            monkeypatch.setattr(runner.vram, "ensure_free", lambda required_mb: None)

            result = runner.run_transcription(job, "/tmp/fake.wav", cfg)
            assert "error" in result
            assert "VRAM" in result["error"]

            updated = JobStore.get_by_id(job.id)
            assert updated.state == JobState.FAILED.value

    def test_run_transcription_success(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Transcript OK")
            runner = WorkflowRunner(JobStore, cfg)

            monkeypatch.setattr(runner.vram, "ensure_free", lambda required_mb: 0)
            monkeypatch.setattr(runner.vram, "track_model", lambda name, gpu, mb: None)

            from transcria.stt.transcription import Transcriber

            fake_result = {
                "transcript_text": "[0s->5s] Bonjour",
                "segment_count": 1,
                "speaker_count": 0,
            }
            monkeypatch.setattr(Transcriber, "transcribe", lambda self, job, path: fake_result)

            result = runner.run_transcription(job, "/tmp/fake.wav", cfg)
            assert result["segment_count"] == 1
            assert result["transcript_text"] == "[0s->5s] Bonjour"

    def test_run_transcription_exception_offloads(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Transcript Crash")
            runner = WorkflowRunner(JobStore, cfg)

            monkeypatch.setattr(runner.vram, "ensure_free", lambda required_mb: 0)

            offload_called = {"v": False}
            def fake_offload():
                offload_called["v"] = True
            monkeypatch.setattr(runner.vram, "offload_all", fake_offload)

            from transcria.stt.transcription import Transcriber

            monkeypatch.setattr(Transcriber, "transcribe", lambda self, job, path: (_ for _ in ()).throw(RuntimeError("STT down")))

            result = runner.run_transcription(job, "/tmp/fake.wav", cfg)
            assert "error" in result
            assert offload_called["v"] is True

            updated = JobStore.get_by_id(job.id)
            assert updated.state == JobState.FAILED.value


class TestWorkflowRunnerRunDiarization:
    def test_run_diarization_success(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Diarize OK")
            runner = WorkflowRunner(JobStore, cfg)

            from transcria.stt.diarization import DiarizerService

            fake_result = {
                "available": True,
                "turns": [{"start": 0, "end": 5, "speaker": "SPEAKER_00"}],
                "speakers": ["SPEAKER_00"],
                "stats": {"SPEAKER_00": {"speaking_time_seconds": 60, "turn_count": 8}},
            }
            monkeypatch.setattr(DiarizerService, "diarize", lambda self, job, path: fake_result)

            audio_path = str(tmp_path / "test.wav")
            with open(audio_path, "w") as f:
                f.write("fake")

            result = runner.run_diarization(job, audio_path, cfg)
            assert result["available"] is True
            assert "SPEAKER_00" in result["speakers"]

    def test_run_diarization_failure(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Diarize Fail")
            runner = WorkflowRunner(JobStore, cfg)

            from transcria.stt.diarization import DiarizerService

            monkeypatch.setattr(DiarizerService, "diarize", lambda self, job, path: (_ for _ in ()).throw(RuntimeError("Pyannote error")))

            audio_path = str(tmp_path / "test.wav")
            with open(audio_path, "w") as f:
                f.write("fake")

            result = runner.run_diarization(job, audio_path, cfg)
            assert "error" in result


class TestWorkflowRunnerRunCorrection:
    def test_run_correction_success(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Correction OK")
            runner = WorkflowRunner(JobStore, cfg)

            monkeypatch.setattr(runner.vram, "free_all_gpus", lambda: True)
            monkeypatch.setattr(runner.vram, "launch_qwen_35b", lambda: True)
            monkeypatch.setattr(runner.vram, "stop_qwen_35b", lambda: True)

            from transcria.gpu.opencode_runner import OpenCodeRunner

            def fake_run_correction(self_runner, srt_path, context_path, lexicon_path):
                return {
                    "success": True,
                    "corrected_srt": "1\n00:00:00,000 --> 00:00:05,000\nBonjour corrigé\n",
                    "report": "# Rapport de correction\n2 corrections appliquées",
                    "error": "",
                }

            monkeypatch.setattr(OpenCodeRunner, "run_correction", fake_run_correction)

            from transcria.jobs.filesystem import JobFilesystem

            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
            fs.save_text("metadata/transcription.srt", "1\n00:00:00,000 --> 00:00:05,000\nBonjour\n")

            result = runner.run_correction(job, cfg)
            assert result["success"] is True
            assert "corrigé" in result["corrected_srt"]

            saved_srt = fs.load_text("metadata/transcription_corrigee.srt")
            assert saved_srt is not None
            assert "corrigé" in saved_srt

    def test_run_correction_qwen_not_available(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Correction No Qwen")
            runner = WorkflowRunner(JobStore, cfg)

            from transcria.jobs.filesystem import JobFilesystem
            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
            fs.save_text("metadata/transcription.srt", "1\n00:00:00,000 --> 00:00:05,000\nTest\n")

            monkeypatch.setattr(runner.vram, "free_all_gpus", lambda: True)
            monkeypatch.setattr(runner.vram, "launch_qwen_35b", lambda: False)

            result = runner.run_correction(job, cfg)
            assert result["success"] is False
            assert "Qwen" in result["error"] or "non disponible" in result["error"]

    def test_run_correction_missing_srt(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Correction No SRT")
            runner = WorkflowRunner(JobStore, cfg)

            result = runner.run_correction(job, cfg)
            assert result["success"] is False
            assert "SRT" in result["error"]

    def test_run_correction_exception_stops_qwen(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Correction Crash")
            runner = WorkflowRunner(JobStore, cfg)

            monkeypatch.setattr(runner.vram, "free_all_gpus", lambda: True)
            monkeypatch.setattr(runner.vram, "launch_qwen_35b", lambda: True)

            stop_called = {"v": False}
            def fake_stop():
                stop_called["v"] = True
            monkeypatch.setattr(runner.vram, "stop_qwen_35b", fake_stop)

            from transcria.gpu.opencode_runner import OpenCodeRunner
            monkeypatch.setattr(OpenCodeRunner, "run_correction", lambda self, s, c, l: (_ for _ in ()).throw(RuntimeError("Qwen crash")))

            from transcria.jobs.filesystem import JobFilesystem
            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
            fs.save_text("metadata/transcription.srt", "1\n00:00:00,000 --> 00:00:05,000\nTest\n")

            result = runner.run_correction(job, cfg)
            assert result["success"] is False
            assert stop_called["v"] is True


class TestWorkflowRunnerRunQualityChecks:
    def test_run_quality_checks_success(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Quality OK")
            runner = WorkflowRunner(JobStore, cfg)

            from transcria.quality.quality_report import QualityReporter

            fake_report = {"quality_score": 85, "total_checks": 5, "checks": []}
            monkeypatch.setattr(QualityReporter, "run_all_checks", lambda self, job: fake_report)

            result = runner.run_quality_checks(job, cfg)
            assert result["quality_score"] == 85

            updated = JobStore.get_by_id(job.id)
            assert updated.state == JobState.QUALITY_CHECKED.value


class TestWorkflowRunnerBuildExport:
    def test_build_export_success(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Export OK")
            runner = WorkflowRunner(JobStore, cfg)

            from transcria.exports.package_builder import PackageBuilder

            fake_result = {"zip_path": "/tmp/test.zip", "zip_name": "test.zip", "size_mb": 1.0}
            monkeypatch.setattr(PackageBuilder, "build_package", lambda self, job: fake_result)
            monkeypatch.setattr(runner.vram, "offload_all", lambda: None)

            result = runner.build_export(job, cfg)
            assert "zip_path" in result

            updated = JobStore.get_by_id(job.id)
            assert updated.state == JobState.EXPORT_READY.value


class TestWorkflowStateEdgeCasesExtended:
    def test_compute_statuses_summary_running(self):
        statuses = WorkflowState.compute_statuses("summary_running")
        assert statuses["summary"] == StepStatus.IN_PROGRESS
        assert statuses["file"] == StepStatus.DONE

    def test_compute_statuses_transcribing(self):
        statuses = WorkflowState.compute_statuses("transcribing")
        assert statuses["processing"] == StepStatus.IN_PROGRESS
        assert statuses["lexicon"] == StepStatus.DONE

    def test_compute_statuses_diarizing(self):
        statuses = WorkflowState.compute_statuses("diarizing")
        assert statuses["processing"] == StepStatus.IN_PROGRESS

    def test_compute_statuses_arbitrating(self):
        statuses = WorkflowState.compute_statuses("arbitrating")
        assert statuses["processing"] == StepStatus.IN_PROGRESS

    def test_compute_statuses_quality_checking(self):
        statuses = WorkflowState.compute_statuses("quality_checking")
        assert statuses["quality"] == StepStatus.IN_PROGRESS
        assert statuses["processing"] == StepStatus.DONE

    def test_compute_statuses_quality_checked(self):
        statuses = WorkflowState.compute_statuses("quality_checked")
        assert statuses["quality"] == StepStatus.DONE
        assert statuses["export"] == StepStatus.IN_PROGRESS

    def test_all_job_states_produce_valid_statuses(self):
        for state in JobState:
            statuses = WorkflowState.compute_statuses(state.value)
            assert isinstance(statuses, dict)
            assert len(statuses) == 9
            for sid, status in statuses.items():
                assert isinstance(status, StepStatus)

    def test_get_next_step_for_each_progress_state(self):
        progress_states = {
            "uploaded": "analyze",
            "analyzed": "summary",
            "summary_running": "summary",
            "summary_done": "context",
            "context_done": "participants",
            "participants_done": "lexicon",
            "speaker_detection_done": "lexicon",
            "lexicon_done": "processing",
            "ready_to_process": "processing",
        }
        for state_val, expected_step in progress_states.items():
            statuses = WorkflowState.compute_statuses(state_val)
            next_s = WorkflowState.get_next_step(statuses)
            assert next_s is not None, f"No next step for state {state_val}"
            assert next_s["id"] == expected_step, f"Expected {expected_step} for {state_val}, got {next_s['id']}"

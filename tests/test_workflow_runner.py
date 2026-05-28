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
            "arbitration_llm": {"model_id": "local/test-llm-arbitrage"},
        },
        "services": {
            "dashboard_llm_url": "http://127.0.0.1:5001",
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

    def test_apply_speaker_roles_does_not_overwrite_user_validated_name(self, tmp_path):
        fs = JobFilesystem(str(tmp_path), "job-speaker-roles-user-name")
        fs.save_json("speakers/speaker_mapping.json", {
            "mapping": {
                "SPEAKER_00": {"name": "martossien", "participant_id": "p1"},
            },
            "speakers": [
                {"speaker_id": "SPEAKER_00", "mapped_name": "martossien", "validation": "user_validated"},
            ],
        })
        fs.save_json("speakers/speaker_stats.json", {
            "speakers": [
                {"speaker_id": "SPEAKER_00", "mapped_name": "martossien", "validation": "user_validated"},
            ],
        })
        fs.save_json("context/participants.json", [
            {"id": "p1", "name": "martossien", "function": "", "service": "", "role": ""},
        ])

        class Log:
            @staticmethod
            def info(*args, **kwargs):
                pass

        WorkflowRunner._apply_speaker_roles(
            fs,
            {"SPEAKER_00": {"label": "Sylvain Martin", "role": "déclarant"}},
            Log(),
        )

        participants = fs.load_json("context/participants.json")
        stats = fs.load_json("speakers/speaker_stats.json")
        mapping = fs.load_json("speakers/speaker_mapping.json")
        assert participants[0]["name"] == "martossien"
        assert participants[0]["role"] == "déclarant"
        assert stats["speakers"][0]["mapped_name"] == "martossien"
        assert mapping["mapping"]["SPEAKER_00"]["name"] == "martossien"
        assert mapping["speakers"][0]["mapped_name"] == "martossien"


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
                    "arbitration_llm": {"model_id": "local/test-llm-arbitrage", "timeout_seconds": 1234, "opencode_bin": "opencode"},
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

            monkeypatch.setattr(runner.vram, "ensure_arbitrage_llm_ready", lambda expected_model_id=None: True)
            monkeypatch.setattr(runner.vram, "free_all_gpus", lambda: True)
            monkeypatch.setattr(runner.vram, "launch_arbitrage_llm", lambda: True)
            monkeypatch.setattr(runner.vram, "stop_arbitrage_llm", lambda: True)

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

    def test_run_correction_filters_session_lexicon_before_llm(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(
                storage={"jobs_dir": str(tmp_path / "jobs")},
                workflow={
                    "enable_quick_summary": True,
                    "enable_speaker_detection": True,
                    "enable_quality_mode": True,
                    "summary_llm": {"enabled": False},
                    "arbitration_llm": {"model_id": "local/test-llm-arbitrage"},
                },
            )
            job = JobStore.create_job(owner_id, "Correction Lexicon Filter")
            runner = WorkflowRunner(JobStore, cfg)

            from transcria.gpu.opencode_runner import OpenCodeRunner

            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
            fs.save_text("metadata/transcription.srt", "1\n00:00:00,000 --> 00:00:05,000\nLe denes répond à l'API.\n")
            fs.save_text("context/job_context.yaml", "meeting: {}\n")
            fs.save_json("context/session_lexicon.json", [
                {"term": "DNS", "variants": ["dénès"], "priority": "normale"},
                {"term": "API", "variants": [], "priority": "normale"},
                {"term": "SI critique", "variants": [], "priority": "critique"},
                {"term": "Absent normal", "variants": [], "priority": "normale"},
            ])

            monkeypatch.setattr(runner.vram, "ensure_arbitrage_llm_ready", lambda expected_model_id=None: True)
            captured = {}

            def fake_run_correction(self, srt_path, context_path, lexicon_path):
                captured["lexicon_path"] = lexicon_path
                with open(lexicon_path, "r", encoding="utf-8") as fh:
                    captured["lexicon"] = json.load(fh)
                return {
                    "success": True,
                    "corrected_srt": "corrigé",
                    "report": "",
                    "warning": "",
                    "error": "",
                }

            monkeypatch.setattr(OpenCodeRunner, "run_correction", fake_run_correction)

            result = runner.run_correction(job, cfg)

            assert result["success"] is True
            assert captured["lexicon_path"].endswith("session_lexicon_filtered.json")
            assert [entry["term"] for entry in captured["lexicon"]] == ["DNS", "API", "SI critique"]
            assert captured["lexicon"][2]["_preservation_only"] is True
            assert fs.load_json("context/session_lexicon.json")[3]["term"] == "Absent normal"


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
    def test_quality_mode_keeps_configured_backend_by_default(self, app, owner_id, tmp_path):
        with app.app_context():
            from transcria.services.pipeline_service import PipelineService

            cfg = _default_config(
                storage={"jobs_dir": str(tmp_path / "jobs")},
                models={"stt_backend": "cohere", "cohere_model_path": "/tmp/fake_model"},
                workflow={
                    "enable_quick_summary": True,
                    "enable_speaker_detection": True,
                    "enable_quality_mode": True,
                    "summary_llm": {"enabled": False},
                    "quality_transcription": {
                        "force_stt_backend": None,
                        "enabled_for_modes": [],
                        "force_on_degraded_summary": False,
                    },
                },
            )
            job = JobStore.create_job(owner_id, "Pipeline Quality Cohere")

            effective = PipelineService(cfg)._config_for_mode("quality", job)

            assert effective["models"]["stt_backend"] == "cohere"

    def test_quality_mode_forces_configured_whisper_backend(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            from transcria.services.pipeline_service import PipelineService

            cfg = _default_config(
                storage={"jobs_dir": str(tmp_path / "jobs")},
                models={"stt_backend": "cohere", "cohere_model_path": "/tmp/fake_model"},
                workflow={
                    "enable_quick_summary": True,
                    "enable_speaker_detection": True,
                    "enable_quality_mode": True,
                    "summary_llm": {"enabled": False},
                    "quality_transcription": {
                        "force_stt_backend": "whisper",
                        "enabled_for_modes": ["quality"],
                    },
                },
            )
            job = JobStore.create_job(owner_id, "Pipeline Quality Whisper")
            service = PipelineService(cfg)
            captured = {}

            def fake_transcription(job_obj, audio_path, config):
                captured["backend"] = config["models"]["stt_backend"]
                return {"segments": []}

            monkeypatch.setattr(service.runner, "run_transcription", fake_transcription)
            monkeypatch.setattr(service.runner, "run_diarization", lambda *args, **kwargs: {})
            monkeypatch.setattr(service.runner, "run_correction", lambda *args, **kwargs: {})
            monkeypatch.setattr(service.runner, "run_quality_checks", lambda *args, **kwargs: {})
            monkeypatch.setattr(service.runner, "build_export", lambda *args, **kwargs: {})

            service.run_process(job, "/tmp/fake.wav", "quality")

            assert captured["backend"] == "whisper"

    def test_degraded_summary_forces_configured_whisper_backend(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            from transcria.services.pipeline_service import PipelineService

            cfg = _default_config(
                storage={"jobs_dir": str(tmp_path / "jobs")},
                models={"stt_backend": "cohere", "cohere_model_path": "/tmp/fake_model"},
                workflow={
                    "enable_quick_summary": True,
                    "enable_speaker_detection": True,
                    "enable_quality_mode": True,
                    "summary_llm": {"enabled": False},
                    "quality_transcription": {
                        "force_stt_backend": "whisper",
                        "enabled_for_modes": ["quality"],
                        "force_on_degraded_summary": True,
                        "degraded_summary_levels": ["degrade"],
                    },
                },
            )
            job = JobStore.create_job(owner_id, "Pipeline Degraded Whisper")
            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
            fs.save_json("summary/summary.json", {"diagnostics": {"level": "degrade"}})

            service = PipelineService(cfg)
            captured = {}

            def fake_transcription(job_obj, audio_path, config):
                captured["backend"] = config["models"]["stt_backend"]
                return {"segments": []}

            monkeypatch.setattr(service.runner, "run_transcription", fake_transcription)
            monkeypatch.setattr(service.runner, "run_correction", lambda *args, **kwargs: {})
            monkeypatch.setattr(service.runner, "run_quality_checks", lambda *args, **kwargs: {})
            monkeypatch.setattr(service.runner, "build_export", lambda *args, **kwargs: {})

            service.run_process(job, "/tmp/fake.wav", "fast")

            assert captured["backend"] == "whisper"

    def test_whisper_backend_injects_session_lexicon_hotwords_when_enabled(self, app, owner_id, tmp_path):
        with app.app_context():
            from transcria.services.pipeline_service import PipelineService

            cfg = _default_config(
                storage={"jobs_dir": str(tmp_path / "jobs")},
                models={"stt_backend": "whisper", "cohere_model_path": "/tmp/fake_model"},
                whisper={
                    "hotwords": "Terme statique",
                    "lexicon_hotwords": {
                        "enabled": True,
                        "priorities": ["critique", "importante"],
                        "max_terms": 3,
                        "max_chars": 120,
                        "prefix": "Termes importants :",
                    },
                },
            )
            job = JobStore.create_job(owner_id, "Pipeline Whisper Hotwords")
            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
            fs.save_json("context/session_lexicon.json", [
                {"term": "EBITDA", "priority": "critique", "source": "central"},
                {"term": "Comité stratégique", "priority": "importante", "source": "session"},
                {"term": "Mot normal", "priority": "normale", "source": "central"},
            ])

            effective = PipelineService(cfg)._config_for_mode("fast", job)
            stats = fs.load_json("metadata/whisper_hotwords.json")

            assert effective["whisper"]["hotwords"] == "Terme statique, EBITDA, Comité stratégique"
            assert stats["candidate_terms"] == 3
            assert stats["injected_terms"] == 2
            assert stats["excluded_by_priority"] == 1

    def test_cohere_backend_does_not_inject_whisper_hotwords(self, app, owner_id, tmp_path):
        with app.app_context():
            from transcria.services.pipeline_service import PipelineService

            cfg = _default_config(
                storage={"jobs_dir": str(tmp_path / "jobs")},
                models={"stt_backend": "cohere", "cohere_model_path": "/tmp/fake_model"},
                whisper={
                    "lexicon_hotwords": {
                        "enabled": True,
                        "priorities": ["critique"],
                        "max_terms": 10,
                        "max_chars": 200,
                        "prefix": "Termes importants :",
                    },
                },
            )
            job = JobStore.create_job(owner_id, "Pipeline Cohere No Hotwords")
            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
            fs.save_json("context/session_lexicon.json", [{"term": "EBITDA", "priority": "critique"}])

            effective = PipelineService(cfg)._config_for_mode("fast", job)

            assert "hotwords" not in effective.get("whisper", {})
            assert fs.load_json("metadata/whisper_hotwords.json") is None

    def test_cohere_backend_injects_contextual_bias_terms_when_enabled(self, app, owner_id, tmp_path):
        with app.app_context():
            from transcria.services.pipeline_service import PipelineService

            cfg = _default_config(
                storage={"jobs_dir": str(tmp_path / "jobs")},
                models={"stt_backend": "cohere", "cohere_model_path": "/tmp/fake_model"},
                cohere={
                    "lexicon_biasing": {
                        "enabled": True,
                        "priorities": ["critique", "importante"],
                        "max_terms": 3,
                        "boost": 0.2,
                        "max_prefix_tokens": 20,
                    },
                },
            )
            job = JobStore.create_job(owner_id, "Pipeline Cohere Biasing")
            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
            fs.save_json("context/session_lexicon.json", [
                {"term": "indemnités", "priority": "critique", "variants": ["inimités"]},
                {"term": "DIF", "priority": "importante"},
                {"term": "mot normal", "priority": "normale"},
            ])

            effective = PipelineService(cfg)._config_for_mode("fast", job)
            stats = fs.load_json("metadata/cohere_lexicon_biasing.json")

            assert effective["cohere"]["_lexicon_bias_terms"] == ["indemnités", "DIF"]
            assert "inimités" not in effective["cohere"]["_lexicon_bias_terms"]
            assert stats["candidate_terms"] == 3
            assert stats["injected_terms"] == 2
            assert stats["excluded_by_priority"] == 1
            assert stats["boost"] == 0.2
            assert stats["start_boost"] == 0.05
            assert stats["max_prefix_tokens"] == 20

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

    def test_pipeline_can_defer_terminal_state_to_worker(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            from transcria.services.pipeline_service import PipelineService

            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Pipeline Deferred Terminal State")
            service = PipelineService(cfg)

            monkeypatch.setattr(
                service.runner,
                "run_transcription",
                lambda job_obj, audio_path, config: {"segments": []},
            )
            monkeypatch.setattr(service.runner, "run_correction", lambda *args, **kwargs: {})
            monkeypatch.setattr(service.runner, "run_quality_checks", lambda *args, **kwargs: {})
            monkeypatch.setattr(service.runner, "build_export", lambda *args, **kwargs: {})

            result = service.run_process(job, "/tmp/fake.wav", "fast", finalize_job_state=False)
            updated = JobStore.get_by_id(job.id)

            assert result["status"] == "completed"
            assert updated.state != JobState.COMPLETED.value


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

            from transcria.workflow import runner as runner_module
            from transcria.stt.summary import SummaryGenerator

            monkeypatch.setattr(runner_module, "GPUSession", FakeSession)
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

            from transcria.jobs.filesystem import JobFilesystem as _JFS

            monkeypatch.setattr(runner.vram, "ensure_free", lambda required_mb: 0)
            monkeypatch.setattr(runner.vram, "untrack_model", lambda name: None)
            monkeypatch.setattr(runner.vram, "offload_all", lambda: None)
            monkeypatch.setattr(runner.vram, "free_all_gpus", lambda: True)
            monkeypatch.setattr(runner.vram, "launch_arbitrage_llm", lambda: True)
            monkeypatch.setattr(runner.vram, "stop_arbitrage_llm", lambda: True)

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
            monkeypatch.setattr(runner.vram, "stop_arbitrage_llm", lambda: True)

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
            monkeypatch.setattr(runner.vram, "launch_arbitrage_llm", lambda: True)
            monkeypatch.setattr(runner.vram, "stop_arbitrage_llm", lambda: True)

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

    def test_run_correction_llm_not_available(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Correction No LLM")
            runner = WorkflowRunner(JobStore, cfg)

            from transcria.jobs.filesystem import JobFilesystem
            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
            fs.save_text("metadata/transcription.srt", "1\n00:00:00,000 --> 00:00:05,000\nTest\n")

            monkeypatch.setattr(runner.vram, "free_all_gpus", lambda: True)
            monkeypatch.setattr(runner.vram, "launch_arbitrage_llm", lambda: False)

            result = runner.run_correction(job, cfg)
            assert result["success"] is False
            assert "non disponible" in result["error"]

    def test_run_correction_missing_srt(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Correction No SRT")
            runner = WorkflowRunner(JobStore, cfg)

            result = runner.run_correction(job, cfg)
            assert result["success"] is False
            assert "SRT" in result["error"]

    def test_run_correction_exception_stops_arbitrage_llm(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Correction Crash")
            runner = WorkflowRunner(JobStore, cfg)

            monkeypatch.setattr(runner.vram, "free_all_gpus", lambda: True)
            monkeypatch.setattr(runner.vram, "launch_arbitrage_llm", lambda: True)

            stop_called = {"v": False}
            def fake_stop():
                stop_called["v"] = True
            monkeypatch.setattr(runner.vram, "stop_arbitrage_llm", fake_stop)

            from transcria.gpu.opencode_runner import OpenCodeRunner
            monkeypatch.setattr(OpenCodeRunner, "run_correction", lambda self, s, c, l: (_ for _ in ()).throw(RuntimeError("LLM crash")))

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


# ---------------------------------------------------------------------------
# Section genre dans diarization_context.md
# ---------------------------------------------------------------------------


class TestDiarizationContextGenderSection:
    """_build_gender_section et son intégration dans _write_diarization_context."""

    def _scene(self, dominant="male", male_ratio=0.70, female_ratio=0.30):
        return {
            "has_music": False,
            "speech_ratio": 0.85,
            "gender": {
                "has_gender_data": True,
                "dominant": dominant,
                "male_ratio": male_ratio,
                "female_ratio": female_ratio,
            },
            "stats": {
                "labels": {
                    "male": {"duration_s": 42.0, "ratio": male_ratio},
                    "female": {"duration_s": 18.0, "ratio": female_ratio},
                },
                "total_duration_s": 60.0,
            },
        }

    def _minimal_speakers_result(self):
        return {
            "speakers": [
                {"speaker_id": "SPEAKER_00", "speaking_time_seconds": 60, "turn_count": 5}
            ],
            "turns": [],
        }

    # --- _build_gender_section : fonction pure ---

    def test_build_gender_section_empty_scene_returns_empty_list(self):
        assert WorkflowRunner._build_gender_section({}) == []

    def test_build_gender_section_no_gender_data_returns_empty_list(self):
        scene = {"gender": {"has_gender_data": False}}
        assert WorkflowRunner._build_gender_section(scene) == []

    def test_build_gender_section_includes_masculine_dominant(self):
        lines = WorkflowRunner._build_gender_section(self._scene("male"))
        combined = "\n".join(lines)
        assert "Masculin" in combined

    def test_build_gender_section_includes_feminine_dominant(self):
        lines = WorkflowRunner._build_gender_section(
            self._scene("female", male_ratio=0.25, female_ratio=0.75)
        )
        combined = "\n".join(lines)
        assert "Féminin" in combined

    def test_build_gender_section_includes_durations_from_stats(self):
        lines = WorkflowRunner._build_gender_section(self._scene())
        combined = "\n".join(lines)
        assert "42.0" in combined
        assert "18.0" in combined

    # --- _write_diarization_context avec / sans audio_scene ---

    def test_write_without_scene_has_no_gender_section(self, tmp_path):
        fs = JobFilesystem(str(tmp_path), "test-gender-job")
        content = WorkflowRunner._write_diarization_context(fs, self._minimal_speakers_result())
        assert content is not None
        assert "Genre vocal estimé" not in content

    def test_write_with_scene_includes_gender_section(self, tmp_path):
        fs = JobFilesystem(str(tmp_path), "test-gender-job")
        content = WorkflowRunner._write_diarization_context(
            fs, self._minimal_speakers_result(), audio_scene=self._scene()
        )
        assert content is not None
        assert "Genre vocal estimé" in content
        assert "Masculin" in content


# ---------------------------------------------------------------------------
# _assign_speaker_genders — fonction pure
# ---------------------------------------------------------------------------


class TestAssignSpeakerGenders:
    """Tests unitaires purs : aucun I/O, aucun mock."""

    def _turns(self):
        return [
            {"speaker": "SPEAKER_00", "start": 0.0, "end": 10.0},
            {"speaker": "SPEAKER_01", "start": 10.0, "end": 20.0},
        ]

    def _gender_segs(self):
        return [
            {"start": 0.5, "end": 8.0, "label": "female"},
            {"start": 10.5, "end": 18.0, "label": "male"},
        ]

    def test_majority_female_assigned_female(self):
        result = WorkflowRunner._assign_speaker_genders(self._gender_segs(), self._turns())
        assert result["SPEAKER_00"]["gender"] == "female"

    def test_majority_male_assigned_male(self):
        result = WorkflowRunner._assign_speaker_genders(self._gender_segs(), self._turns())
        assert result["SPEAKER_01"]["gender"] == "male"

    def test_below_min_overlap_returns_empty_gender(self):
        short_segs = [{"start": 0.0, "end": 0.3, "label": "female"}]
        turns = [{"speaker": "SPEAKER_00", "start": 0.0, "end": 5.0}]
        result = WorkflowRunner._assign_speaker_genders(short_segs, turns, min_overlap_s=1.0)
        assert result["SPEAKER_00"]["gender"] == ""

    def test_empty_turns_returns_empty_dict(self):
        assert WorkflowRunner._assign_speaker_genders(self._gender_segs(), []) == {}

    def test_empty_gender_segments_returns_empty_dict(self):
        assert WorkflowRunner._assign_speaker_genders([], self._turns()) == {}

    def test_multiple_speakers_independently_assigned(self):
        result = WorkflowRunner._assign_speaker_genders(self._gender_segs(), self._turns())
        assert set(result.keys()) == {"SPEAKER_00", "SPEAKER_01"}

    def test_partial_overlap_accumulated_correctly(self):
        segs = [
            {"start": 0.0, "end": 3.0, "label": "female"},
            {"start": 3.0, "end": 8.0, "label": "male"},
        ]
        turns = [{"speaker": "SPEAKER_00", "start": 0.0, "end": 10.0}]
        result = WorkflowRunner._assign_speaker_genders(segs, turns, min_overlap_s=1.0)
        assert result["SPEAKER_00"]["female_s"] == pytest.approx(3.0, abs=0.01)
        assert result["SPEAKER_00"]["male_s"] == pytest.approx(5.0, abs=0.01)
        assert result["SPEAKER_00"]["gender"] == "male"

    def test_tie_returns_empty_gender(self):
        segs = [
            {"start": 0.0, "end": 5.0, "label": "female"},
            {"start": 5.0, "end": 10.0, "label": "male"},
        ]
        turns = [{"speaker": "SPEAKER_00", "start": 0.0, "end": 10.0}]
        result = WorkflowRunner._assign_speaker_genders(segs, turns, min_overlap_s=1.0)
        assert result["SPEAKER_00"]["gender"] == ""


# ---------------------------------------------------------------------------
# _inject_speaker_genders — intégration (fs sur disque, mock get_structured_logger)
# ---------------------------------------------------------------------------


class TestInjectSpeakerGenders:
    """Vérifie que _inject_speaker_genders met à jour speaker_stats.json correctement."""

    def _make_runner(self, app, owner_id, tmp_path, monkeypatch):
        return WorkflowRunner(JobStore, _default_config())

    def _make_fs(self, tmp_path, job_id="inject-gender-test"):
        return JobFilesystem(str(tmp_path), job_id)

    def _audio_scene_with_segs(self):
        return {
            "gender_segments": [
                {"start": 0.0, "end": 5.0, "label": "female"},
                {"start": 10.0, "end": 18.0, "label": "male"},
            ]
        }

    def _speakers_result(self):
        return {
            "speakers": [
                {
                    "speaker_id": "SPEAKER_00",
                    "speaking_time_seconds": 10,
                    "turn_count": 2,
                    "turns": [
                        {"start": 0.0, "end": 5.0},
                        {"start": 5.0, "end": 9.0},
                    ],
                },
                {
                    "speaker_id": "SPEAKER_01",
                    "speaking_time_seconds": 8,
                    "turn_count": 1,
                    "turns": [{"start": 10.0, "end": 18.0}],
                },
            ],
            "turns": [],
        }

    def _save_turns(self, fs, turns: list):
        """Écrit speaker_turns.json au format produit par DiarizerService."""
        fs.save_json("speakers/speaker_turns.json", {"available": True, "turns": turns})

    def test_inject_updates_speaker_stats(self, app, owner_id, tmp_path, monkeypatch):
        runner = self._make_runner(app, owner_id, tmp_path, monkeypatch)
        fs = self._make_fs(tmp_path)
        fs.save_json("speakers/speaker_stats.json", {
            "speakers": [
                {"speaker_id": "SPEAKER_00", "gender": "", "speaking_time_seconds": 10},
                {"speaker_id": "SPEAKER_01", "gender": "", "speaking_time_seconds": 8},
            ]
        })
        self._save_turns(fs, [
            {"start": 0.0, "end": 10.0, "speaker": "SPEAKER_00"},
            {"start": 10.0, "end": 18.0, "speaker": "SPEAKER_01"},
        ])

        result = runner._inject_speaker_genders(fs, self._audio_scene_with_segs())

        assert isinstance(result, dict)
        updated = fs.load_json("speakers/speaker_stats.json")
        by_id = {s["speaker_id"]: s for s in updated["speakers"]}
        assert by_id["SPEAKER_00"]["gender"] == "female"
        assert by_id["SPEAKER_01"]["gender"] == "male"

    def test_inject_does_not_overwrite_user_gender(self, app, owner_id, tmp_path, monkeypatch):
        runner = self._make_runner(app, owner_id, tmp_path, monkeypatch)
        fs = self._make_fs(tmp_path, "no-overwrite-test")
        fs.save_json("speakers/speaker_stats.json", {
            "speakers": [{"speaker_id": "SPEAKER_00", "gender": "male"}]
        })
        scene = {"gender_segments": [{"start": 0.0, "end": 10.0, "label": "female"}]}
        self._save_turns(fs, [{"start": 0.0, "end": 10.0, "speaker": "SPEAKER_00"}])

        runner._inject_speaker_genders(fs, scene)

        updated = fs.load_json("speakers/speaker_stats.json")
        assert updated["speakers"][0]["gender"] == "male"

    def test_inject_no_gender_segments_skips(self, app, owner_id, tmp_path, monkeypatch):
        runner = self._make_runner(app, owner_id, tmp_path, monkeypatch)
        fs = self._make_fs(tmp_path, "no-segs-test")
        fs.save_json("speakers/speaker_stats.json", {
            "speakers": [{"speaker_id": "SPEAKER_00", "gender": ""}]
        })

        result = runner._inject_speaker_genders(fs, {})

        assert result == {}
        stats = fs.load_json("speakers/speaker_stats.json")
        assert stats["speakers"][0]["gender"] == ""

    def test_inject_returns_speaker_genders_dict(self, app, owner_id, tmp_path, monkeypatch):
        runner = self._make_runner(app, owner_id, tmp_path, monkeypatch)
        fs = self._make_fs(tmp_path, "return-dict-test")
        fs.save_json("speakers/speaker_stats.json", {
            "speakers": [
                {"speaker_id": "SPEAKER_00", "gender": ""},
                {"speaker_id": "SPEAKER_01", "gender": ""},
            ]
        })
        self._save_turns(fs, [
            {"start": 0.0, "end": 10.0, "speaker": "SPEAKER_00"},
            {"start": 10.0, "end": 18.0, "speaker": "SPEAKER_01"},
        ])

        result = runner._inject_speaker_genders(fs, self._audio_scene_with_segs())

        assert "SPEAKER_00" in result
        assert "SPEAKER_01" in result
        for v in result.values():
            assert "gender" in v
            assert "male_s" in v
            assert "female_s" in v


# ---------------------------------------------------------------------------
# _write_diarization_context avec speaker_genders
# ---------------------------------------------------------------------------


class TestWriteDiarizationContextWithSpeakerGenders:
    """Section per-speaker genre dans le contexte LLM."""

    def _minimal_speakers_result(self):
        return {
            "speakers": [
                {"speaker_id": "SPEAKER_00", "speaking_time_seconds": 20, "turn_count": 3},
                {"speaker_id": "SPEAKER_01", "speaking_time_seconds": 15, "turn_count": 2},
            ],
            "turns": [],
        }

    def test_with_speaker_genders_includes_per_speaker_section(self, tmp_path):
        fs = JobFilesystem(str(tmp_path), "per-spk-gender-test")
        speaker_genders = {
            "SPEAKER_00": {"gender": "female", "female_s": 18.7, "male_s": 3.2},
            "SPEAKER_01": {"gender": "male", "female_s": 1.1, "male_s": 12.4},
        }

        content = WorkflowRunner._write_diarization_context(
            fs, self._minimal_speakers_result(), speaker_genders=speaker_genders
        )

        assert content is not None
        assert "Genre vocal par locuteur" in content
        assert "SPEAKER_00" in content
        assert "Féminin" in content
        assert "SPEAKER_01" in content
        assert "Masculin" in content

    def test_without_speaker_genders_no_per_speaker_section(self, tmp_path):
        fs = JobFilesystem(str(tmp_path), "no-per-spk-test")

        content = WorkflowRunner._write_diarization_context(
            fs, self._minimal_speakers_result()
        )

        assert content is not None
        assert "Genre vocal par locuteur" not in content

    def test_empty_speaker_genders_no_section(self, tmp_path):
        fs = JobFilesystem(str(tmp_path), "empty-per-spk-test")

        content = WorkflowRunner._write_diarization_context(
            fs, self._minimal_speakers_result(), speaker_genders={}
        )

        assert content is not None
        assert "Genre vocal par locuteur" not in content

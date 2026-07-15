"""Tests de la phase DIARISATION (workflow/phases/diarization.py) — migrés de test_workflow_runner.py (B1 lot 2)."""
from transcria.workflow.runner import WorkflowRunner
from transcria.jobs.models import JobState
from transcria.jobs.store import JobStore


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

    def test_run_speaker_detection_update_state_false_keeps_state(self, app, owner_id, monkeypatch, tmp_path):
        """En sous-phase de résumé (update_state=False), l'état global n'est pas touché.

        Régression BUG-001 : pendant run_summary la diarisation ne doit pas faire passer
        le job par SPEAKER_DETECTION_RUNNING/DONE (états « en avant » du wizard), sinon
        compute_statuses marque summary=DONE et affiche un cadre contexte vide.
        """
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Speaker Detect Subphase")
            JobStore.update_state(job.id, JobState.SUMMARY_RUNNING)
            runner = WorkflowRunner(JobStore, cfg)

            from transcria.stt.speaker_detection import SpeakerDetector

            monkeypatch.setattr(
                SpeakerDetector, "detect",
                lambda self, job, audio_path, device="cpu": {"available": True, "speakers": [{"speaker_id": "SPEAKER_00"}]},
            )
            import torch
            monkeypatch.setattr(torch.cuda, "is_available", lambda: False)

            audio_path = str(tmp_path / "test.wav")
            with open(audio_path, "w") as f:
                f.write("fake")

            result = runner.run_speaker_detection(job, audio_path, cfg, update_state=False)
            assert result["available"] is True

            updated = JobStore.get_by_id(job.id)
            assert updated.state == JobState.SUMMARY_RUNNING.value

    def test_run_speaker_detection_failure_update_state_false_keeps_state(self, app, owner_id, monkeypatch, tmp_path):
        """Un échec pyannote en sous-phase de résumé reste best-effort : aucun FAILED global."""
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Speaker Detect Subphase Fail")
            JobStore.update_state(job.id, JobState.SUMMARY_RUNNING)
            runner = WorkflowRunner(JobStore, cfg)

            from transcria.stt.speaker_detection import SpeakerDetector

            monkeypatch.setattr(
                SpeakerDetector, "detect",
                lambda self, job, audio_path, device="cpu": (_ for _ in ()).throw(RuntimeError("No GPU")),
            )
            import torch
            monkeypatch.setattr(torch.cuda, "is_available", lambda: False)

            audio_path = str(tmp_path / "test.wav")
            with open(audio_path, "w") as f:
                f.write("fake")

            result = runner.run_speaker_detection(job, audio_path, cfg, update_state=False)
            assert "error" in result

            updated = JobStore.get_by_id(job.id)
            assert updated.state == JobState.SUMMARY_RUNNING.value


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


class TestPyannoteProgressCallback:
    """Contrat du callback de progression pyannote : fenêtres 50–70 (résumé) / 60–70 (final)."""

    def _runner_with_recorder(self, cfg):
        runner = WorkflowRunner(JobStore, cfg)
        calls = []

        class _Recorder:
            def update(self, job_id, **kwargs):
                calls.append(kwargs)

        runner.progress = _Recorder()
        return runner, calls

    def test_percent_mapping_summary_and_processing(self, app, owner_id):
        with app.app_context():
            cfg = _default_config()
            job = JobStore.create_job(owner_id, "Callback pyannote")
            runner, calls = self._runner_with_recorder(cfg)

            runner._pyannote_progress_callback(job, "summary")("segmentation", 50.0)
            runner._pyannote_progress_callback(job, "processing")("embeddings", 100.0)
            runner._pyannote_progress_callback(job, "processing")("chargement", None)

            assert calls[0]["percent"] == 60.0  # summary : base 50, fenêtre 20
            assert calls[1]["percent"] == 70.0  # processing : base 60, fenêtre 10
            assert calls[2]["percent"] is None
            assert all(c["phase"] == "pyannote" for c in calls)
            assert "segmentation" in calls[0]["message"]

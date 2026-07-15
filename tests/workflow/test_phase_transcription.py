"""Tests de la phase TRANSCRIPTION (workflow/phases/transcription.py) — migrés de test_workflow_runner.py (B1 lot 2)."""
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


class TestWorkflowRunnerRunTranscription:
    def test_run_transcription_vram_insufficient(self, app, owner_id, monkeypatch, tmp_path):
        """_reserve_gpu_phase retourne None → signal `vram_wait` (PAS FAILED).

        VRAM transitoire : run_transcription remonte `vram_wait` ; le pipeline propage
        et l'exécuteur re-queue le job (reprise auto), sans état terminal.
        """
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Transcript VRAM Fail")
            runner = WorkflowRunner(JobStore, cfg)

            monkeypatch.setattr(runner, "_reserve_gpu_phase", lambda job, required_mb, phase: (None, False))

            result = runner.run_transcription(job, "/tmp/fake.wav", cfg)
            assert result.get("vram_wait") is True
            assert result.get("required_mb")
            assert result.get("phase") == "stt"

            updated = JobStore.get_by_id(job.id)
            # Pas d'état terminal sur VRAM : le job sera re-queué (reprise auto). Le
            # pipeline redémarre du début, donc l'état TRANSCRIBING courant est sans
            # conséquence ; seul compte le fait qu'il N'EST PAS FAILED.
            assert updated.state != JobState.FAILED.value

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
        """Sur exception STT, _release_gpu_phase appelle offload_all (chemin VRAMManager).

        managed_by_allocator=False force le chemin offload_all dans _release_gpu_phase,
        car en présence d'un GPU réel l'allocateur réussit et prendrait le chemin
        release_phase (qui n'appelle pas offload_all).
        """
        from types import SimpleNamespace

        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Transcript Crash")
            runner = WorkflowRunner(JobStore, cfg)

            # managed_by_allocator=False → _release_gpu_phase appellera offload_all
            monkeypatch.setattr(
                runner, "_reserve_gpu_phase",
                lambda job, required_mb, phase: (SimpleNamespace(gpu_index=0), False),
            )

            offload_called = {"v": False}
            def fake_offload():
                offload_called["v"] = True
            monkeypatch.setattr(runner.vram, "offload_all", fake_offload)

            from transcria.stt.transcription import Transcriber

            monkeypatch.setattr(Transcriber, "transcribe", lambda self, job, path: (_ for _ in ()).throw(RuntimeError("STT down")))

            result = runner.run_transcription(job, "/tmp/fake.wav", cfg)
            assert "error" in result
            assert offload_called["v"] is True, "offload_all doit être appelé sur exception (chemin VRAMManager)"

            updated = JobStore.get_by_id(job.id)
            assert updated.state == JobState.FAILED.value

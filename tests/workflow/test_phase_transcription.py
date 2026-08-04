"""Tests de la phase TRANSCRIPTION (workflow/phases/transcription.py) — migrés de test_workflow_runner.py (B1 lot 2)."""
from types import SimpleNamespace

from transcria.jobs.models import JobState
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


class TestWorkflowRunnerRunTranscription:
    def test_run_transcription_vram_insufficient(self, app, owner_id, monkeypatch, tmp_path):
        """_reserve_gpu_phase retourne None → signal `vram_wait` (PAS FAILED).

        VRAM transitoire : run_transcription remonte `vram_wait` ; le pipeline propage
        et l'exécuteur re-queue le job (reprise auto), sans état terminal.
        """
        # Jamais de kill de port RÉEL (garde conftest _no_real_process_kills) : le chemin
        # vram_wait libère le port LLM pour de vrai — sur une machine où un serveur
        # écoute 8080, ce test le tuait (incident 2026-08-03, 7 tests attrapés d'un coup).
        monkeypatch.setattr("transcria.gpu.vram_manager.VRAMManager._kill_port",
                            lambda self, port: True)
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

            # P2 (audit 2026-07-30) : la voie sans comptabilité est fermée — on simule
            # désormais la VRAIE porte (allocateur), plus VRAMManager.ensure_free.
            monkeypatch.setattr(runner.allocator, "try_reserve",
                                lambda job_id, mb, phase, preferred_gpu=None: SimpleNamespace(gpu_index=0))
            monkeypatch.setattr(runner.allocator, "release_phase", lambda job_id, phase: None)

            from transcria.stt.transcription import Transcriber

            fake_result = {
                "segments": [{"start": 0.0, "end": 5.0, "text": "Bonjour"}],
                "transcript_text": "[0s->5s] Bonjour",
                "segment_count": 1,
                "speaker_count": 0,
            }
            monkeypatch.setattr(Transcriber, "transcribe", lambda self, job, path: fake_result)

            result = runner.run_transcription(job, "/tmp/fake.wav", cfg)
            assert result["segment_count"] == 1
            assert result["transcript_text"] == "[0s->5s] Bonjour"

    def test_run_transcription_exception_releases_reservation(self, app, owner_id, monkeypatch, tmp_path):
        """Sur exception STT, la RÉSERVATION allocateur de la phase est libérée.

        P2 (audit 2026-07-30) : l'ancien test vérifiait `offload_all` sur le chemin
        VRAMManager sans comptabilité — chemin fermé (l'offload vidait un cache CUDA
        dans le mauvais process). L'invariant qui reste : jamais de réservation qui
        fuit après un crash de phase.
        """
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Transcript Crash")
            runner = WorkflowRunner(JobStore, cfg)

            released = {"v": False}
            monkeypatch.setattr(runner.allocator, "try_reserve",
                                lambda job_id, mb, phase, preferred_gpu=None: SimpleNamespace(gpu_index=0))
            monkeypatch.setattr(runner.allocator, "release_phase",
                                lambda job_id, phase: released.__setitem__("v", True))

            from transcria.stt.transcription import Transcriber

            monkeypatch.setattr(Transcriber, "transcribe", lambda self, job, path: (_ for _ in ()).throw(RuntimeError("STT down")))

            result = runner.run_transcription(job, "/tmp/fake.wav", cfg)
            assert "error" in result
            assert released["v"] is True, "la réservation doit être libérée sur exception"

            updated = JobStore.get_by_id(job.id)
            assert updated.state == JobState.FAILED.value


class TestTranscriptVideCourtCircuit:
    def test_zero_segment_echoue_clair_avant_les_phases_llm(self, app, owner_id, monkeypatch, tmp_path):
        """Vérité terrain bruit blanc (2026-08-04) : 0 segment partait quand même en
        correction LLM (3 tentatives + exception). Désormais : FAILED immédiat avec
        un constat actionnable pour l'utilisateur."""
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Bruit pur")
            runner = WorkflowRunner(JobStore, cfg)
            monkeypatch.setattr(runner.allocator, "try_reserve",
                                lambda job_id, mb, phase, preferred_gpu=None: SimpleNamespace(gpu_index=0))
            monkeypatch.setattr(runner.allocator, "release_phase", lambda job_id, phase: None)

            from transcria.stt.transcription import Transcriber
            monkeypatch.setattr(Transcriber, "transcribe",
                                lambda self, job, path: {"segments": [], "srt_content": "",
                                                         "speaker_count": 0})

            result = runner.run_transcription(job, "/tmp/fake.wav", cfg)
            assert "Aucune parole détectée" in result.get("error", "")
            updated = JobStore.get_by_id(job.id)
            assert updated.state == JobState.FAILED.value
            assert "Aucune parole détectée" in (updated.error_message or updated.error or "")

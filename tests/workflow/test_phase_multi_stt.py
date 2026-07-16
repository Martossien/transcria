"""Tests de la phase MULTI-STT ciblée (workflow/phases/multi_stt_review.py) — B1 lot 2.

La logique pure (sélection/arbitrage/application) est testée dans
tests/test_multi_stt_review.py ; ici on teste l'ORCHESTRATION best-effort :
réservation GPU secondaire, verrou LLM, application des remplacements —
sans GPU réel (transcripteur, librosa et chat_completion substitués à la source).
"""
from types import SimpleNamespace

from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.store import JobStore
from transcria.workflow.runner import WorkflowRunner

_SEGMENTS = [
    {"start": 0.5, "end": 4.0, "text": "segment propre"},
    {"start": 10.0, "end": 14.0, "text": "segment degrade"},
]
_DIFFICULTY_MAP = [
    {"start": 0.0, "end": 9.0, "difficulty": "ok", "signals": []},
    {"start": 9.0, "end": 18.0, "difficulty": "degrade", "signals": ["snr_faible"]},
]


def _config(tmp_path, **multi_stt):
    return {
        "storage": {"jobs_dir": str(tmp_path / "jobs")},
        "workflow": {"multi_stt": {"enabled": True, **multi_stt}},
        "models": {"stt_backend": "cohere", "cohere_model_path": "/tmp/fake_model"},
        "services": {"arbitrage_script": "/bin/true", "stop_script": "/bin/true"},
    }


class _FakeTranscriber:
    def __init__(self, text="segment retranscrit"):
        self.text = text
        self.offloaded = False

    def transcribe(self, path, language=None, audio_array=None, sample_rate=None):
        return [{"text": self.text}]

    def offload(self):
        self.offloaded = True

    def segments_to_srt(self, segments, mapping=None):
        return "\n".join(str(s.get("text") or "") for s in segments)


class TestRunMultiSttReview:
    def _prepared(self, app_cfg, owner_id, monkeypatch, *, fake_transcriber=None):
        job = JobStore.create_job(owner_id, "Multi STT")
        runner = WorkflowRunner(JobStore, app_cfg)
        fs = JobFilesystem(app_cfg["storage"]["jobs_dir"], job.id)
        fs.save_json("metadata/transcription_segments.json", [dict(s) for s in _SEGMENTS])
        fs.save_json("metadata/audio_preflight.json", {"difficulty_map": _DIFFICULTY_MAP})

        monkeypatch.setattr(
            runner, "_reserve_gpu_phase",
            lambda job, mb, phase: (SimpleNamespace(gpu_index=0), False),
        )
        monkeypatch.setattr(runner, "_release_gpu_phase", lambda job, phase, managed: None)
        monkeypatch.setattr(runner.allocator, "try_acquire_llm", lambda job_id, timeout_s=120: True)
        monkeypatch.setattr(runner.allocator, "release_llm", lambda job_id: None)
        monkeypatch.setattr(runner, "_should_reserve_llm_vram", lambda: False)
        monkeypatch.setattr(runner.vram, "is_arbitrage_llm_running", lambda: True)
        monkeypatch.setattr(runner.vram, "ensure_arbitrage_llm_ready", lambda expected_model_id=None: True)

        if fake_transcriber is not None:
            # C5 : la phase importe create_transcriber en tête — patcher le consommateur.
            from transcria.workflow.phases import multi_stt_review

            monkeypatch.setattr(
                multi_stt_review, "create_transcriber",
                lambda config, backend=None, device=None: fake_transcriber,
            )
            import librosa
            import numpy as np

            monkeypatch.setattr(librosa, "load", lambda path, sr=16000, mono=True: (np.zeros(sr * 20), sr))
        return job, runner, fs

    def test_skipped_when_disabled(self, app, owner_id, tmp_path):
        with app.app_context():
            cfg = _config(tmp_path)
            cfg["workflow"]["multi_stt"]["enabled"] = False
            job = JobStore.create_job(owner_id, "Multi STT off")
            result = WorkflowRunner(JobStore, cfg).run_multi_stt_review(job, "/tmp/a.wav", cfg)
            assert result == {"success": True, "skipped": True, "reason": "disabled"}

    def test_skipped_without_degraded_segments(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _config(tmp_path)
            job, runner, fs = self._prepared(cfg, owner_id, monkeypatch)
            fs.save_json("metadata/audio_preflight.json", {"difficulty_map": []})
            result = runner.run_multi_stt_review(job, "/tmp/a.wav", cfg)
            assert result["reason"] == "no_degraded_segments"

    def test_skipped_on_vram_shortage_for_secondary_backend(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _config(tmp_path)
            job, runner, fs = self._prepared(cfg, owner_id, monkeypatch)
            monkeypatch.setattr(runner, "_reserve_gpu_phase", lambda job, mb, phase: (None, False))
            monkeypatch.setattr(runner, "_reclaim_vram_from_idle_arbitrage_llm", lambda sl: False)
            result = runner.run_multi_stt_review(job, "/tmp/a.wav", cfg)
            assert result["reason"] == "vram_insufficient"

    def test_skipped_when_secondary_produces_nothing(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _config(tmp_path)
            fake = _FakeTranscriber(text="")
            job, runner, fs = self._prepared(cfg, owner_id, monkeypatch, fake_transcriber=fake)
            result = runner.run_multi_stt_review(job, "/tmp/a.wav", cfg)
            assert result["reason"] == "no_secondary_text"
            assert fake.offloaded is True
            trace = fs.load_json("metadata/multi_stt.json")
            assert trace["secondary_texts"] == 0 and trace["decisions"] == []

    def test_skipped_when_llm_lock_busy(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _config(tmp_path)
            fake = _FakeTranscriber()
            job, runner, fs = self._prepared(cfg, owner_id, monkeypatch, fake_transcriber=fake)
            monkeypatch.setattr(runner.allocator, "try_acquire_llm", lambda job_id, timeout_s=120: False)
            result = runner.run_multi_stt_review(job, "/tmp/a.wav", cfg)
            assert result["reason"] == "llm_busy"

    def test_success_replaces_segment_when_llm_chooses_b(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _config(tmp_path)
            fake = _FakeTranscriber(text="segment retranscrit")
            job, runner, fs = self._prepared(cfg, owner_id, monkeypatch, fake_transcriber=fake)

            # C5 : la phase importe chat_completion en tête — patcher le consommateur.
            from transcria.workflow.phases import multi_stt_review

            monkeypatch.setattr(
                multi_stt_review, "chat_completion",
                lambda config, messages, timeout_s=120, max_tokens=16: "B",
            )

            result = runner.run_multi_stt_review(job, "/tmp/a.wav", cfg)

            assert result["success"] is True
            assert result["candidates"] == 1 and result["arbitrated"] == 1 and result["replaced"] == 1
            segments = fs.load_json("metadata/transcription_segments.json")
            assert segments[1]["text"] == "segment retranscrit"
            assert segments[1]["multi_stt"]["choice"] == "secondary"
            assert "segment retranscrit" in (fs.load_text("metadata/transcription.srt") or "")
            trace = fs.load_json("metadata/multi_stt.json")
            assert trace["replaced"] == 1 and trace["decisions"][0]["choice"] == "B"

    def test_doubt_keeps_primary_text(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _config(tmp_path)
            fake = _FakeTranscriber(text="segment retranscrit")
            job, runner, fs = self._prepared(cfg, owner_id, monkeypatch, fake_transcriber=fake)

            # C5 : la phase importe chat_completion en tête — patcher le consommateur.
            from transcria.workflow.phases import multi_stt_review

            monkeypatch.setattr(
                multi_stt_review, "chat_completion",
                lambda config, messages, timeout_s=120, max_tokens=16: "réponse illisible",
            )

            result = runner.run_multi_stt_review(job, "/tmp/a.wav", cfg)

            assert result["replaced"] == 0  # doute → choix « A », texte principal conservé
            segments = fs.load_json("metadata/transcription_segments.json")
            assert segments[1]["text"] == "segment degrade"

    def test_unexpected_error_never_breaks_pipeline(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _config(tmp_path)
            job = JobStore.create_job(owner_id, "Multi STT erreur")
            runner = WorkflowRunner(JobStore, cfg)
            class _BrokenFs:
                def load_json(self, rel):
                    raise RuntimeError("disque HS")

            monkeypatch.setattr(runner, "_get_fs", lambda config, job_id: _BrokenFs())
            result = runner.run_multi_stt_review(job, "/tmp/a.wav", cfg)
            assert result == {"success": True, "skipped": True, "reason": "error"}

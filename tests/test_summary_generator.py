"""Tests for SummaryGenerator — generate_quick_summary."""
import numpy as np
import pytest

from transcria.stt.summary import SummaryGenerator


def _default_cfg(tmp_path):
    return {
        "storage": {"jobs_dir": str(tmp_path / "jobs")},
        "workflow": {"summary_llm": {"enabled": False}},
        "models": {"cohere_model_path": "/tmp/fake_model"},
    }


_FAKE_AUDIO = np.zeros(16000, dtype=np.float32)   # 1s de silence synthétique


class TestSummaryGeneratorGenerateQuickSummary:
    def test_generate_quick_summary_saves_files_and_returns(self, app, owner_id, tmp_path, monkeypatch):
        with app.app_context():
            cfg = _default_cfg(tmp_path)
            from transcria.jobs.store import JobStore
            from transcria.jobs.filesystem import JobFilesystem
            from transcria.stt.cohere_transcriber import CohereTranscriber
            from transcria.audio.vad import SileroVAD
            import librosa

            job = JobStore.create_job(owner_id, "Quick Summary")
            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)

            fake_segments = [
                {"start": 0.0, "end": 5.0, "text": "Bonjour à tous"},
                {"start": 5.0, "end": 10.0, "text": "Le budget est bouclé"},
            ]

            # Mock audio I/O et pipeline IA — pas de vrai fichier WAV requis
            monkeypatch.setattr(librosa, "load", lambda *a, **kw: (_FAKE_AUDIO, 16000))
            monkeypatch.setattr(SileroVAD, "build_speech_chunks",
                                lambda self, audio, **kw: [{"start": 0.0, "end": 1.0, "audio": audio}])
            monkeypatch.setattr(CohereTranscriber, "load", lambda self: True)
            monkeypatch.setattr(CohereTranscriber, "transcribe", lambda self, *a, **kw: fake_segments)
            monkeypatch.setattr(CohereTranscriber, "offload", lambda self: None)

            gen = SummaryGenerator(cfg)
            audio_path = tmp_path / "test_audio.wav"
            audio_path.write_text("fake audio")

            result = gen.generate_quick_summary(job, audio_path, gpu_index=0)

            assert "transcript_text" in result
            assert "Bonjour à tous" in result["transcript_text"]
            assert result["segment_count"] == 2

            transcript_file = fs.load_text("summary/quick_transcript.txt")
            assert transcript_file is not None
            assert "Bonjour à tous" in transcript_file

            summary_json = fs.load_json("summary/summary.json")
            assert summary_json is not None
            assert len(summary_json["segments"]) == 2

            summary_md = fs.load_text("summary/summary.md")
            assert summary_md is not None
            assert "Résumé" in summary_md or "Extrait" in summary_md
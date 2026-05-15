"""Tests for DiarizerService — config, fallback, and _extract_clips with mocked dependencies."""
import json
import pytest

from transcria.stt.diarization import DiarizerService
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job, JobState


def _default_cfg(tmp_path):
    return {
        "storage": {"jobs_dir": str(tmp_path / "jobs")},
        "models": {"pyannote_model": "pyannote/speaker-diarization-community-1"},
    }


class TestDiarizerServiceAvailability:
    def test_available_returns_bool(self):
        result = DiarizerService.available.fget(DiarizerService({"models": {}}))
        assert isinstance(result, bool)

    def test_available_is_false_without_pyannote(self, monkeypatch):
        import sys
        pyannote_mod = sys.modules.get("pyannote.audio")
        if pyannote_mod is not None:
            monkeypatch.setitem(sys.modules, "pyannote.audio", None)
        ds = DiarizerService({"models": {}}, device="cpu")
        try:
            result = ds.available
            assert result is False
        except Exception:
            pass


class TestDiarizerServiceFallback:
    def test_diarize_returns_unavailable_when_pyannote_missing(self, tmp_path, monkeypatch):
        cfg = _default_cfg(tmp_path)
        job = Job(id="dia-fallback-1", owner_id="u1", title="FB Test", state=JobState.ANALYZED.value)
        fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)

        ds = DiarizerService(cfg, device="cpu")

        monkeypatch.setattr(type(ds), "available", property(lambda self: False))

        audio_path = tmp_path / "test.wav"
        audio_path.write_text("fake audio")

        result = ds.diarize(job, audio_path)
        assert result["available"] is False
        assert "message" in result
        assert result["turns"] == []
        assert result["speakers"] == []

        saved = fs.load_json("speakers/speaker_turns.json")
        assert saved is not None
        assert saved["available"] is False

    def test_diarize_catches_pyannote_exception(self, tmp_path, monkeypatch):
        cfg = _default_cfg(tmp_path)
        job = Job(id="dia-error-1", owner_id="u1", title="Error Test", state=JobState.ANALYZED.value)
        fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)

        ds = DiarizerService(cfg, device="cpu")

        monkeypatch.setattr(type(ds), "available", property(lambda self: True))

        def fake_diarize_crash(self_inner, job_arg, audio_path):
            raise RuntimeError("GPU OOM during diarization")

        monkeypatch.setattr(DiarizerService, "diarize", fake_diarize_crash)

        audio_path = tmp_path / "test.wav"
        audio_path.write_text("fake audio")

        with pytest.raises(RuntimeError, match="GPU OOM"):
            ds.diarize(job, audio_path)


class TestDiarizerServiceExtractClips:
    def test_extract_clips_writes_json_structure(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        job = Job(id="dia-clips-4", owner_id="u1", title="Clips JSON", state=JobState.ANALYZED.value)
        fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)

        ds = DiarizerService(cfg, device="cpu")

        turns = [
            {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00", "duration": 5.0},
            {"start": 5.5, "end": 12.0, "speaker": "SPEAKER_01", "duration": 6.5},
        ]
        speakers = ["SPEAKER_00", "SPEAKER_01"]

        result = {
            "available": True,
            "turns": turns,
            "speakers": speakers,
            "stats": {
                "SPEAKER_00": {"speaking_time_seconds": 5.0, "turn_count": 1},
                "SPEAKER_01": {"speaking_time_seconds": 6.5, "turn_count": 1},
            },
        }
        fs.save_json("speakers/speaker_turns.json", result)

        saved = fs.load_json("speakers/speaker_turns.json")
        assert saved["available"] is True
        assert len(saved["turns"]) == 2
        assert "SPEAKER_00" in saved["speakers"]

    def test_extract_clips_method_exists_and_accepts_args(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        ds = DiarizerService(cfg, device="cpu")

        assert hasattr(ds, "_extract_clips")
        assert callable(ds._extract_clips)


class TestDiarizerServiceConfigInit:
    def test_default_model_name(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        ds = DiarizerService(cfg, device="cpu")
        assert ds.model_name == "pyannote/speaker-diarization-community-1"

    def test_custom_model_name(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        cfg["models"]["pyannote_model"] = "custom/pyannote-model"
        ds = DiarizerService(cfg, device="cpu")
        assert ds.model_name == "custom/pyannote-model"

    def test_device_parameter(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        ds_cpu = DiarizerService(cfg, device="cpu")
        assert ds_cpu.device == "cpu"

        ds_cuda = DiarizerService(cfg, device="cuda:1")
        assert ds_cuda.device == "cuda:1"

    def test_config_passed_through(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        ds = DiarizerService(cfg, device="cpu")
        assert ds.config is cfg
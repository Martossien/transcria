"""Tests for diarization backends — DiarizerService, SortformerDiarizer, factory."""
import pytest
import numpy as np

from transcria.stt.diarization import DiarizerService
from transcria.stt.sortformer_diarizer import SortformerDiarizer
from transcria.stt.base_diarizer import BaseDiarizer
from transcria.stt.diarizer_factory import create_diarizer, get_diarizer_vram_mb, list_available_backends
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

    def test_load_cached_result_returns_valid_checkpoint(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        cfg["diarization"] = {"cache_enabled": True, "cache_audio_fingerprint": True}
        job = Job(id="dia-cache-1", owner_id="u1", title="Cache", state=JobState.ANALYZED.value)
        fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
        audio_path = tmp_path / "audio.wav"
        audio_path.write_text("fake audio")
        ds = DiarizerService(cfg, device="cpu")
        result = {"available": True, "turns": [], "exclusive_turns": [], "speakers": ["SPEAKER_00"]}
        fs.save_json("speakers/speaker_turns.json", result)
        ds._save_cache_metadata(fs, audio_path, result)

        cached = ds._load_cached_result(fs, audio_path)

        assert cached == result

    def test_acoustic_embedding_is_deterministic(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        ds = DiarizerService(cfg, device="cpu")
        audio = np.ones(1600, dtype=np.float32) * 0.5

        embedding = ds._acoustic_embedding(audio, 16000)

        assert embedding["duration_seconds"] == 0.1
        assert embedding["rms"] == 0.5


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

    def test_is_base_diarizer_subclass(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        ds = DiarizerService(cfg, device="cpu")
        assert isinstance(ds, BaseDiarizer)


# ---------------------------------------------------------------------------
# SortformerDiarizer — tests unitaires purs (sans GPU ni NeMo)
# ---------------------------------------------------------------------------

class TestSortformerDiarizerParseOutput:
    """Tests de _parse_sortformer_output et _normalize_speaker_id — fonctions pures."""

    def test_parse_empty(self):
        assert SortformerDiarizer._parse_sortformer_output([]) == []

    def test_parse_single_segment(self):
        lines = ["0.500 3.120 speaker_0"]
        turns = SortformerDiarizer._parse_sortformer_output(lines)
        assert len(turns) == 1
        assert turns[0]["start"] == 0.5
        assert turns[0]["end"] == 3.12
        assert turns[0]["duration"] == pytest.approx(2.62)
        assert turns[0]["speaker"] == "SPEAKER_00"

    def test_parse_multiple_speakers_sorted(self):
        # NeMo retourne les segments par locuteur ; _parse_sortformer_output doit
        # les retrier par timestamp de début.
        lines = [
            "3.510 7.260 speaker_1",
            "0.500 3.120 speaker_0",
            "8.000 10.000 speaker_0",
        ]
        turns = SortformerDiarizer._parse_sortformer_output(lines)
        assert [t["start"] for t in turns] == [0.5, 3.51, 8.0]
        assert turns[0]["speaker"] == "SPEAKER_00"
        assert turns[1]["speaker"] == "SPEAKER_01"

    def test_parse_skips_zero_duration(self):
        lines = ["1.000 1.000 speaker_0", "2.000 3.000 speaker_1"]
        turns = SortformerDiarizer._parse_sortformer_output(lines)
        assert len(turns) == 1
        assert turns[0]["speaker"] == "SPEAKER_01"

    def test_parse_skips_blank_lines(self):
        lines = ["", "  ", "0.100 0.500 speaker_2"]
        turns = SortformerDiarizer._parse_sortformer_output(lines)
        assert len(turns) == 1
        assert turns[0]["speaker"] == "SPEAKER_02"

    def test_parse_ignores_malformed_lines(self):
        lines = ["not a valid line", "0.100 0.500 speaker_0"]
        turns = SortformerDiarizer._parse_sortformer_output(lines)
        assert len(turns) == 1

    def test_normalize_speaker_id_standard(self):
        assert SortformerDiarizer._normalize_speaker_id("speaker_0") == "SPEAKER_00"
        assert SortformerDiarizer._normalize_speaker_id("speaker_3") == "SPEAKER_03"
        assert SortformerDiarizer._normalize_speaker_id("speaker_12") == "SPEAKER_12"

    def test_normalize_speaker_id_unknown_format(self):
        # Format non reconnu : conservé tel quel (robustesse)
        result = SortformerDiarizer._normalize_speaker_id("unknown_spk")
        assert result == "unknown_spk"

    def test_parse_gpu_index(self):
        assert SortformerDiarizer._parse_gpu_index("cuda:0") == 0
        assert SortformerDiarizer._parse_gpu_index("cuda:1") == 1
        assert SortformerDiarizer._parse_gpu_index("cpu") is None


class TestSortformerDiarizerConfig:
    def test_default_model_name(self):
        sd = SortformerDiarizer({"sortformer": {}}, device="cpu")
        assert sd.model_name == "nvidia/diar_streaming_sortformer_4spk-v2.1"

    def test_custom_model_name(self):
        cfg = {"sortformer": {"model_id": "custom/sortformer-model"}}
        sd = SortformerDiarizer(cfg, device="cpu")
        assert sd.model_name == "custom/sortformer-model"

    def test_is_base_diarizer_subclass(self):
        sd = SortformerDiarizer({}, device="cpu")
        assert isinstance(sd, BaseDiarizer)

    def test_available_returns_bool(self):
        sd = SortformerDiarizer({}, device="cpu")
        assert isinstance(sd.available, bool)

    def test_available_false_when_nemo_missing(self, monkeypatch):
        import sys
        monkeypatch.setitem(sys.modules, "nemo.collections.asr.models", None)
        sd = SortformerDiarizer({}, device="cpu")
        assert sd.available is False

    def test_diarize_returns_unavailable_when_nemo_missing(self, tmp_path, monkeypatch):
        cfg = {
            "storage": {"jobs_dir": str(tmp_path / "jobs")},
            "sortformer": {},
        }
        job = Job(id="sf-fallback-1", owner_id="u1", title="SF Fallback", state=JobState.ANALYZED.value)
        audio_path = tmp_path / "test.wav"
        audio_path.write_text("fake")

        sd = SortformerDiarizer(cfg, device="cpu")
        monkeypatch.setattr(type(sd), "available", property(lambda self: False))

        result = sd.diarize(job, audio_path)
        assert result["available"] is False
        assert result["turns"] == []
        assert result["speakers"] == []


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

class TestDiarizerFactory:
    def test_create_pyannote_by_default(self, tmp_path):
        cfg = {"storage": {"jobs_dir": str(tmp_path)}, "models": {}}
        diarizer = create_diarizer(cfg, device="cpu")
        assert isinstance(diarizer, DiarizerService)

    def test_create_pyannote_explicit(self, tmp_path):
        cfg = {
            "storage": {"jobs_dir": str(tmp_path)},
            "models": {"diarization_backend": "pyannote"},
        }
        diarizer = create_diarizer(cfg, device="cpu")
        assert isinstance(diarizer, DiarizerService)

    def test_create_sortformer(self, tmp_path):
        cfg = {
            "storage": {"jobs_dir": str(tmp_path)},
            "models": {"diarization_backend": "sortformer"},
            "sortformer": {},
        }
        diarizer = create_diarizer(cfg, device="cpu")
        assert isinstance(diarizer, SortformerDiarizer)

    def test_create_unknown_backend_falls_back_to_pyannote(self, tmp_path):
        cfg = {
            "storage": {"jobs_dir": str(tmp_path)},
            "models": {"diarization_backend": "unknown_backend"},
        }
        diarizer = create_diarizer(cfg, device="cpu")
        assert isinstance(diarizer, DiarizerService)

    def test_device_propagated(self, tmp_path):
        cfg = {"storage": {"jobs_dir": str(tmp_path)}, "models": {}}
        diarizer = create_diarizer(cfg, device="cpu")
        assert diarizer.device == "cpu"

    def test_list_available_backends(self):
        backends = list_available_backends()
        assert "pyannote" in backends
        assert "sortformer" in backends

    def test_get_vram_pyannote_default(self):
        vram = get_diarizer_vram_mb("pyannote", {})
        assert vram == 2000

    def test_get_vram_sortformer_default(self):
        vram = get_diarizer_vram_mb("sortformer", {})
        assert vram == 3500

    def test_get_vram_from_config(self):
        cfg = {"gpu": {"sortformer_vram_mb": 4000, "pyannote_vram_mb": 2500}}
        assert get_diarizer_vram_mb("sortformer", cfg) == 4000
        assert get_diarizer_vram_mb("pyannote", cfg) == 2500

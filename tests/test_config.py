import os
import tempfile
import importlib.util
from pathlib import Path

import pytest

from transcria.config import get_config_path, load_config, save_config, _deep_merge
from transcria.config.config_schema import validate_config


class TestConfigLoading:
    def test_default_config_structure(self):
        cfg = load_config()
        assert "server" in cfg
        assert "storage" in cfg
        assert "auth" in cfg
        assert "services" in cfg
        assert "models" in cfg
        assert "workflow" in cfg
        assert "security" in cfg

    def test_default_values(self):
        cfg = load_config()
        assert cfg["server"]["port"] == 7870
        assert cfg["models"]["default_stt_model"] == "cohere-transcribe-03-2026"
        assert cfg["whisper"]["model_size"] == "large-v3"
        assert cfg["whisper"]["condition_on_previous_text"] is False
        assert cfg["whisper"]["forced_alignment"]["backend"] == "torchaudio_ctc"
        assert cfg["auth"]["enabled"] is True
        assert cfg["security"]["max_upload_size_mb"] == 1024
        assert ".mp3" in cfg["security"]["allowed_upload_extensions"]

    def test_load_from_yaml_file(self):
        content = """server:
  port: 9999
auth:
  first_admin_password: "secret"
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(content)
            f.flush()
            cfg = load_config(f.name)
        os.unlink(f.name)
        assert cfg["server"]["port"] == 9999
        assert cfg["auth"]["first_admin_password"] == "secret"
        assert cfg["server"]["host"] == "0.0.0.0"

    def test_auth_enabled_false_is_not_effective(self):
        content = """auth:
  enabled: false
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(content)
            f.flush()
            cfg = load_config(f.name)
        os.unlink(f.name)
        assert cfg["auth"]["enabled"] is True

    def test_deep_merge_nested(self):
        base = {"a": {"b": 1, "c": 2}, "d": 3}
        over = {"a": {"b": 10}}
        result = _deep_merge(base, over)
        assert result["a"]["b"] == 10
        assert result["a"]["c"] == 2
        assert result["d"] == 3

    def test_deep_merge_new_key(self):
        base = {"a": 1}
        over = {"b": 2}
        result = _deep_merge(base, over)
        assert result["a"] == 1
        assert result["b"] == 2

    def test_env_var_override(self):
        old = os.environ.get("TRANSCRIA_CONFIG")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("server:\n  port: 8888\n")
            f.flush()
            path = f.name
        try:
            os.environ["TRANSCRIA_CONFIG"] = path
            cfg = load_config()
            assert cfg["server"]["port"] == 8888
        finally:
            os.unlink(path)
            if old is not None:
                os.environ["TRANSCRIA_CONFIG"] = old
            else:
                os.environ.pop("TRANSCRIA_CONFIG", None)

    def test_missing_file_returns_default(self):
        saved = os.environ.pop("TRANSCRIA_CONFIG", None)
        try:
            cfg = load_config("/tmp/nonexistent_transcria_test_config.yaml")
            assert cfg["server"]["port"] == 7870
        finally:
            if saved is not None:
                os.environ["TRANSCRIA_CONFIG"] = saved

    def test_save_config_writes_yaml(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            path = f.name
        try:
            save_config({"server": {"port": 9991}}, path)
            cfg = load_config(path)
            assert cfg["server"]["port"] == 9991
            assert cfg["server"]["host"] == "0.0.0.0"
        finally:
            os.unlink(path)

    def test_save_config_normalizes_auth_enabled(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            path = f.name
        try:
            save_config({"auth": {"enabled": False}}, path)
            with open(path, "r", encoding="utf-8") as fh:
                saved = fh.read()
            assert "enabled: true" in saved
        finally:
            os.unlink(path)

    def test_load_config_allows_long_llm_timeouts(self):
        content = """workflow:
  summary_llm:
    enabled: true
    model_id: "local/test-llm"
    api_base: "http://127.0.0.1:8080/v1"
    timeout_seconds: 1800
  arbitration_llm:
    enabled: true
    model_id: "local/test-llm-arbitrage"
    api_base: "http://127.0.0.1:8080/v1"
    timeout_seconds: 7200
    opencode_bin: "opencode"
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(content)
            f.flush()
            cfg = load_config(f.name)
        os.unlink(f.name)
        assert cfg["workflow"]["summary_llm"]["timeout_seconds"] == 1800
        assert cfg["workflow"]["arbitration_llm"]["timeout_seconds"] == 7200

    def test_legacy_enable_vad_without_vad_section_is_preserved(self):
        content = """workflow:
  enable_vad: false
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(content)
            f.flush()
            cfg = load_config(f.name)
        os.unlink(f.name)
        assert cfg["workflow"]["vad"]["enabled_summary"] is False
        assert cfg["workflow"]["vad"]["enabled_final"] is False

    def test_explicit_vad_section_overrides_legacy_enable_vad(self):
        content = """workflow:
  enable_vad: true
  vad:
    enabled_summary: true
    enabled_final: false
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(content)
            f.flush()
            cfg = load_config(f.name)
        os.unlink(f.name)
        assert cfg["workflow"]["vad"]["enabled_summary"] is True
        assert cfg["workflow"]["vad"]["enabled_final"] is False

    def test_legacy_vllm_port_maps_to_cleanup_ports(self):
        content = """services:
  vllm_port: 8123
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(content)
            f.flush()
            cfg = load_config(f.name)
        os.unlink(f.name)
        assert cfg["services"]["llm_cleanup_ports"] == [8123]

    def test_get_config_path_uses_env(self):
        old = os.environ.get("TRANSCRIA_CONFIG")
        try:
            os.environ["TRANSCRIA_CONFIG"] = "/tmp/transcrIA-custom.yaml"
            assert get_config_path() == "/tmp/transcrIA-custom.yaml"
        finally:
            if old is not None:
                os.environ["TRANSCRIA_CONFIG"] = old
            else:
                os.environ.pop("TRANSCRIA_CONFIG", None)


class TestAppDebugResolution:
    def test_create_app_accepts_explicit_config_path(self):
        from app import create_app

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(
                "server:\n"
                "  debug: false\n"
                "storage:\n"
                "  database_url: \"sqlite:///:memory:\"\n"
                "  jobs_dir: /tmp/transcria_test_jobs_create_app\n"
                "auth:\n"
                "  first_admin_username: admin\n"
                "  first_admin_password: admin-change-me\n"
            )
            f.flush()
            path = f.name
        try:
            app = create_app(path)
            assert app.config["SQLALCHEMY_DATABASE_URI"] == "sqlite:///:memory:"
        finally:
            os.unlink(path)

    def test_no_debug_cli_overrides_config_and_env(self):
        from app import resolve_debug_flag

        assert resolve_debug_flag(False, "true", True) is False

    def test_debug_env_overrides_config_when_cli_not_set(self):
        from app import resolve_debug_flag

        assert resolve_debug_flag(None, "false", True) is False
        assert resolve_debug_flag(None, "true", False) is True

    def test_config_debug_used_without_cli_or_env(self):
        from app import resolve_debug_flag

        assert resolve_debug_flag(None, None, True) is True


class TestBootstrapConfig:
    def test_validate_config_accepts_execution_section(self):
        cfg = load_config()
        cfg["workflow"]["execution"] = {"max_concurrent_jobs": 1}
        result = validate_config(cfg)
        assert result.is_valid

    def test_validate_config_accepts_audio_scene_quality_thresholds(self):
        cfg = load_config()
        cfg["workflow"]["audio_quality"].update({
            "scene_affects_quality_score": False,
            "max_scene_music_ratio": 0.15,
            "max_scene_noise_ratio": 0.20,
            "max_scene_no_energy_ratio": 0.30,
            "min_scene_speech_ratio": 0.55,
            "max_scene_problem_segments": 3,
        })

        result = validate_config(cfg)

        assert result.is_valid

    def test_validate_config_accepts_audio_scene_filter_section(self):
        cfg = load_config()
        cfg["workflow"]["audio_scene_filter"] = {
            "enabled": False,
            "enabled_for_modes": ["quality"],
            "target_labels": ["music", "noise"],
            "min_segment_s": 2.0,
            "min_total_muted_s": 2.0,
            "edge_keep_s": 0.15,
            "max_intervals": 100,
            "timeout_s": 300,
        }

        result = validate_config(cfg)

        assert result.is_valid

    def test_validate_config_accepts_audio_normalization_section(self):
        cfg = load_config()
        cfg["workflow"]["audio_normalization"] = {
            "enabled": False,
            "enabled_for_modes": ["quality"],
            "loudnorm_enabled": True,
            "target_i": -23.0,
            "true_peak": -2.0,
            "lra": 11.0,
            "highpass_hz": None,
            "timeout_s": 300,
        }

        result = validate_config(cfg)

        assert result.is_valid

    def test_validate_config_accepts_audio_preflight_section(self):
        cfg = load_config()
        cfg["workflow"]["audio_preflight"] = {
            "enabled": True,
            "frame_ms": 30,
            "low_rms_threshold": 0.02,
            "very_low_rms_threshold": 0.008,
            "silence_rms_threshold": 0.003,
            "low_snr_db_threshold": 6.0,
            "narrowband_hz_threshold": 3800.0,
            "clipping_threshold": 0.98,
            "clipping_ratio_threshold": 0.001,
        }

        result = validate_config(cfg)

        assert result.is_valid

    def test_validate_config_accepts_audio_decision_extensions(self):
        cfg = load_config()
        cfg["workflow"]["segment_reliability"] = {
            "enabled": True,
            "no_speech_prob_threshold": 0.5,
            "low_word_confidence_ratio": 0.5,
            "low_word_confidence_min": 0.4,
            "micro_segment_s": 0.35,
            "short_segment_s": 0.8,
        }
        cfg["workflow"]["pyannote_chunking"] = {
            "merge_micro_chunks": True,
            "micro_chunk_s": 0.35,
            "micro_chunk_neighbor_gap_s": 0.4,
            "isolated_min_chunk_s": 0.3,
            "padding_s": 0.15,
            "max_chunk_s": 30,
            "min_chunk_s": 1.5,
        }
        cfg["workflow"]["audio_denoise"] = {
            "enabled": False,
            "enabled_for_modes": ["quality"],
            "backend": "ffmpeg_afftdn",
            "force": False,
            "trigger_flags": ["snr_faible"],
            "noise_reduction_db": 12.0,
            "noise_floor_db": -25.0,
            "timeout_s": 300,
        }

        result = validate_config(cfg)

        assert result.is_valid

    def test_default_config_declares_auto_loudnorm_threshold(self):
        cfg = load_config()
        assert cfg["workflow"]["audio_normalization"]["auto_loudnorm_rms_threshold"] == 0.02

    def test_validate_config_rejects_invalid_max_upload_size(self):
        cfg = load_config()
        cfg["security"]["max_upload_size_mb"] = 0

        result = validate_config(cfg)

        assert not result.is_valid
        assert any("security.max_upload_size_mb" in msg for msg in result.errors)

    def test_validate_config_rejects_invalid_cohere_section(self):
        cfg = load_config()
        cfg["cohere"]["collapse_repetition_loops"] = "yes"
        cfg["cohere"]["max_new_tokens"] = 0

        result = validate_config(cfg)

        assert not result.is_valid
        assert any("cohere.collapse_repetition_loops" in msg for msg in result.errors)
        assert any("cohere.max_new_tokens" in msg for msg in result.errors)

    def test_validate_config_rejects_invalid_audio_scene_section(self):
        cfg = load_config()
        cfg["workflow"]["audio_scene"]["detect_gender"] = "true"
        cfg["workflow"]["audio_scene"]["thresholds"]["energy_ratio"] = "low"

        result = validate_config(cfg)

        assert not result.is_valid
        assert any("workflow.audio_scene.detect_gender" in msg for msg in result.errors)
        assert any("workflow.audio_scene.thresholds.energy_ratio" in msg for msg in result.errors)

    def test_validate_config_rejects_invalid_source_separation_section(self):
        cfg = load_config()
        cfg["workflow"]["source_separation"]["backend"] = "spleeter"
        cfg["workflow"]["source_separation"]["stem"] = "voice"
        cfg["workflow"]["source_separation"]["decision"]["min_score"] = "high"

        result = validate_config(cfg)

        assert not result.is_valid
        assert any("workflow.source_separation.backend" in msg for msg in result.errors)
        assert any("workflow.source_separation.stem" in msg for msg in result.errors)
        assert any("workflow.source_separation.decision.min_score" in msg for msg in result.errors)

    def test_validate_config_rejects_invalid_cleanup_quality_and_vad_auto(self):
        cfg = load_config()
        cfg["workflow"]["transcription_cleanup"]["merge_short_segments"] = "false"
        cfg["workflow"]["transcription_cleanup"]["subtitle_artifact_patterns"] = [123]
        cfg["workflow"]["vad"]["auto_enable_final_on_degraded"] = "true"
        cfg["workflow"]["vad"]["auto_enable_final_levels"] = ["degrade", ""]
        cfg["quality"]["thresholds"]["no_speech_prob_threshold"] = "0.5"

        result = validate_config(cfg)

        assert not result.is_valid
        assert any("workflow.transcription_cleanup.merge_short_segments" in msg for msg in result.errors)
        assert any("workflow.transcription_cleanup.subtitle_artifact_patterns[0]" in msg for msg in result.errors)
        assert any("workflow.vad.auto_enable_final_on_degraded" in msg for msg in result.errors)
        assert any("workflow.vad.auto_enable_final_levels[1]" in msg for msg in result.errors)
        assert any("quality.thresholds.no_speech_prob_threshold" in msg for msg in result.errors)

    def test_bootstrap_config_generates_output(self, tmp_path):
        module_path = Path(__file__).resolve().parents[1] / "scripts" / "bootstrap_config.py"
        spec = importlib.util.spec_from_file_location("bootstrap_config", module_path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        example_path = Path(__file__).resolve().parents[1] / "config.example.yaml"
        output_path = tmp_path / "config.generated.yaml"

        merged, messages = module.bootstrap_config(example_path, output_path, force=True)

        assert output_path.is_file()
        assert merged["storage"]["jobs_dir"].endswith("jobs")
        assert "workflow" in merged
        assert isinstance(messages, list)

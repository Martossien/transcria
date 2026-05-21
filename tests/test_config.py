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

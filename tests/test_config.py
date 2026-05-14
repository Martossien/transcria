import os
import tempfile

import pytest

from transcria.config import get_config_path, load_config, save_config, _deep_merge


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

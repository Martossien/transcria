"""Tests unitaires de la phase « Ollama » de l'installateur.

Réseau (curl|sh), sous-processus (ollama pull), détection binaire et prompt interactif
sont injectés : on vérifie l'orchestration (garde GPU / présent / absent+refus /
absent+install / pull ok|ko / écriture config) sans aucun effet réel.
"""
from __future__ import annotations

import io
from pathlib import Path

from transcria.config.yaml_file import get_yaml_value, load_yaml_file
from transcria.installer.console import Console
from transcria.installer.ollama_phase import (
    OllamaPlan,
    apply_ollama,
    ollama_model_for_tier,
)


def _console() -> Console:
    return Console(io.StringIO(), color=False)


class _Runner:
    """Runner injecté : renvoie un code par commande (clé = 1ᵉʳ token significatif)."""

    def __init__(self, codes: dict[str, int] | None = None) -> None:
        self.codes = codes or {}
        self.calls: list[list[str]] = []

    def __call__(self, cmd, check=False, env=None):
        self.calls.append(list(cmd))
        key = "install" if cmd[:2] == ["/bin/sh", "-c"] else cmd[1] if cmd[0] == "ollama" else cmd[0]
        rc = self.codes.get(key, 0)

        class _CP:
            returncode = rc

        return _CP()

    def ran(self, needle: str) -> bool:
        return any(needle in " ".join(c) for c in self.calls)


def _plan(tmp_path: Path, **kw) -> OllamaPlan:
    defaults = dict(config_path=tmp_path / "config.yaml", model="qwen3:8b", gpu_present=True, interactive=False)
    defaults.update(kw)
    return OllamaPlan(**defaults)


def _config(tmp_path: Path) -> dict:
    return load_yaml_file(tmp_path / "config.yaml")


class TestTierMapping:
    def test_known_tier(self):
        assert ollama_model_for_tier("12gb") == "qwen3:8b"
        assert ollama_model_for_tier("32gb") == "qwen3:32b"

    def test_unknown_or_empty_falls_back(self):
        assert ollama_model_for_tier(None) == "qwen3:8b"
        assert ollama_model_for_tier("999gb") == "qwen3:8b"


class TestGpuGuard:
    def test_skips_when_no_gpu(self, tmp_path):
        runner = _Runner()
        res = apply_ollama(_plan(tmp_path, gpu_present=False), console=_console(), runner=runner,
                           has_command=lambda n: False)
        assert res.actions == ["gpu-absent"]
        assert runner.calls == []  # rien d'installé/tiré
        assert not (tmp_path / "config.yaml").exists()  # aucune config écrite


class TestInstallBranch:
    def test_reuses_existing_ollama(self, tmp_path):
        runner = _Runner()
        res = apply_ollama(_plan(tmp_path), console=_console(), runner=runner, has_command=lambda n: True)
        assert "ollama-present" in res.actions
        assert not runner.ran("install.sh")  # pas de réinstallation

    def test_installs_when_absent_noninteractive(self, tmp_path):
        runner = _Runner()
        present = {"ollama": False, "zstd": True}
        res = apply_ollama(_plan(tmp_path), console=_console(), runner=runner,
                           has_command=lambda n: present.get(n, False))
        assert "installed" in res.actions
        assert runner.ran("ollama.com/install.sh")

    def test_pins_version_via_env(self, tmp_path):
        captured = {}

        def runner(cmd, check=False, env=None):
            if cmd[:2] == ["/bin/sh", "-c"]:
                captured["env"] = env

            class _CP:
                returncode = 0

            return _CP()

        apply_ollama(_plan(tmp_path, pin_version="0.5.7"), console=_console(), runner=runner,
                     has_command=lambda n: n != "ollama")
        assert captured["env"] == {"OLLAMA_VERSION": "0.5.7"}

    def test_interactive_decline_stops(self, tmp_path):
        runner = _Runner()
        res = apply_ollama(_plan(tmp_path, interactive=True), console=_console(), runner=runner,
                           has_command=lambda n: False, confirm=lambda _p: False)
        assert res.actions == ["install-declined"]
        assert not (tmp_path / "config.yaml").exists()

    def test_install_failure_aborts_before_config(self, tmp_path):
        runner = _Runner(codes={"install": 1})
        res = apply_ollama(_plan(tmp_path), console=_console(), runner=runner, has_command=lambda n: n == "zstd")
        assert res.actions[-1] == "install-failed"
        assert not (tmp_path / "config.yaml").exists()


class TestConfigWriting:
    def test_writes_backend_keys(self, tmp_path):
        runner = _Runner()
        apply_ollama(_plan(tmp_path, model="qwen3:8b"), console=_console(), runner=runner, has_command=lambda n: True)
        cfg = _config(tmp_path)
        assert get_yaml_value(cfg, "services.backend") == "ollama"
        assert get_yaml_value(cfg, "services.ollama_url") == "http://127.0.0.1:11434"
        assert get_yaml_value(cfg, "services.ollama_model") == "qwen3:8b"
        # model_id opencode = local/<modèle> (le runner splitte et envoie le nom nu).
        assert get_yaml_value(cfg, "workflow.arbitration_llm.model_id") == "local/qwen3:8b"
        assert get_yaml_value(cfg, "workflow.arbitration_llm.api_base") == "http://127.0.0.1:11434/v1"

    def test_pull_runs_and_config_written(self, tmp_path):
        runner = _Runner()
        res = apply_ollama(_plan(tmp_path), console=_console(), runner=runner, has_command=lambda n: True)
        assert runner.ran("ollama pull qwen3:8b")
        assert res.actions[-1] == "configured"

    def test_config_still_written_when_pull_fails(self, tmp_path):
        runner = _Runner(codes={"pull": 1})
        res = apply_ollama(_plan(tmp_path), console=_console(), runner=runner, has_command=lambda n: True)
        assert "pull-failed" in res.actions
        assert res.actions[-1] == "configured"  # config cohérente malgré l'échec de pull
        assert get_yaml_value(_config(tmp_path), "services.backend") == "ollama"

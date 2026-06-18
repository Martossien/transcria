from __future__ import annotations

import os
import stat
from pathlib import Path

import yaml

from transcria.install_arbitrage import apply_profile, render_wrapper, status


def _make_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    profiles = repo / "scripts" / "arbitrage_profiles"
    profiles.mkdir(parents=True)
    profile = profiles / "48gb_test.sh"
    profile.write_text("#!/usr/bin/env bash\nexec llama-server \"$@\"\n", encoding="utf-8")
    profile.chmod(0o755)
    config = repo / "config.yaml"
    config.write_text(
        """services:
  arbitrage_script: ./scripts/launch_arbitrage.sh
gpu:
  llm_vram_mb: 1
  llm_gpu_indices:
  - 9
  llm_vram_mb_per_gpu:
  - 1
""",
        encoding="utf-8",
    )
    return repo, config


def test_render_wrapper_sets_local_defaults_without_literal_quotes(tmp_path):
    profile = tmp_path / "profile's dir" / "launch.sh"

    content = render_wrapper(
        profile_path=profile,
        models_dir="/models with spaces",
        llama_server="/opt/llama's/bin/llama-server",
        gpu_indices=[0, 1],
    )

    assert "export MODELS_DIR='/models with spaces'" in content
    assert "export LLAMA_SERVER='/opt/llama'\"'\"'s/bin/llama-server'" in content
    assert 'export MODELS_DIR="${MODELS_DIR:-' not in content
    assert 'export ARBITRAGE_GPU="${ARBITRAGE_GPU:-0,1}"' in content
    assert "exec '/" in content


def test_apply_profile_generates_wrapper_and_updates_config(tmp_path):
    repo, config = _make_repo(tmp_path)

    output = apply_profile(
        repo_root=repo,
        config_path=config,
        tier="48gb",
        models_dir="/srv/models",
        llama_server="/opt/llama/bin/llama-server",
    )

    assert output == (repo / "scripts" / "generated" / "launch_arbitrage.local.sh").resolve()
    wrapper = output.read_text(encoding="utf-8")
    assert "Fichier généré localement" in wrapper
    assert "export MODELS_DIR='/srv/models'" in wrapper
    assert "export LLAMA_SERVER='/opt/llama/bin/llama-server'" in wrapper
    assert str((repo / "scripts" / "arbitrage_profiles" / "48gb_test.sh").resolve()) in wrapper
    assert stat.S_IMODE(os.stat(output).st_mode) & stat.S_IXUSR

    cfg = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert cfg["services"]["arbitrage_script"] == str(output)
    assert cfg["gpu"]["llm_vram_mb"] == 48000
    assert cfg["gpu"]["llm_gpu_indices"] == [0, 1]
    assert cfg["gpu"]["llm_vram_mb_per_gpu"] == [24000, 24000]


def test_status_reports_configured_script(tmp_path):
    repo, config = _make_repo(tmp_path)

    lines = status(repo_root=repo, config_path=config)

    assert lines[0] == "services.arbitrage_script: ./scripts/launch_arbitrage.sh"
    assert "script introuvable" in lines[1]

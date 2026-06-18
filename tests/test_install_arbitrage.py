from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
import yaml

from transcria.install_arbitrage import apply_profile, render_setup_log, render_wrapper, status


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


def test_render_setup_log_for_llm_selection_events():
    assert render_setup_log(event="profile-skipped", profile="web") == "INFO:Profil web : LLM d'arbitrage locale non requise\n"
    assert render_setup_log(event="vram-too-low", value="8192") == "WARN:VRAM totale 8192 Mio (< 12 Go) — pas de LLM d'arbitrage local.\n"
    assert render_setup_log(event="raw-mode") == (
        "INFO:TranscrIA fonctionnera en TRANSCRIPTION BRUTE (résumé/correction LLM désactivés).\n"
    )
    assert render_setup_log(event="opencode-missing") == (
        "WARN:opencode absent — LLM d'arbitrage non configurable (transcription brute).\n"
    )
    assert render_setup_log(event="opencode-install-later") == (
        "INFO:Installez opencode puis relancez, ou utilisez scripts/switch_arbitrage_llm.sh plus tard.\n"
    )
    assert render_setup_log(event="vram-status", value="49152", gpu_count="2", max_mb="24576") == (
        "OK:VRAM : total 49152 Mio sur 2 GPU (plus grande carte 24576 Mio)\n"
    )
    assert render_setup_log(event="planner-fallback") == (
        "WARN:Planner de placement indisponible — recommandation par VRAM totale (moins fiable).\n"
    )
    assert render_setup_log(event="no-tier") == (
        "WARN:Aucun palier LLM ne tient sur cette topologie — transcription brute conseillée.\n"
    )
    assert render_setup_log(event="recommended-tier", tier="24", label="Qwen test") == (
        "INFO:Palier recommandé : 24 Go → Qwen test\n"
    )
    assert render_setup_log(event="tiers-info") == (
        "INFO:Paliers : 12 / 16 / 24 / 32 / 48 / 64 (Go) — laisser vide pour ignorer.\n"
    )


def test_render_setup_log_for_llm_download_and_activation_events():
    assert render_setup_log(event="llama-qualified", value="/opt/llama-server", tier="9632", label="git") == (
        "OK:llama-server qualifié : /opt/llama-server (build 9632, source git)\n"
    )
    assert render_setup_log(event="llama-unusable", value="/opt/llama-server", tier="too-old") == (
        "WARN:llama-server trouvé mais NON utilisable (too-old) : /opt/llama-server\n"
    )
    assert render_setup_log(event="llama-ld-hint", value="/opt/llama/lib") == (
        "WARN:Libs llama hors chemins standard — exportez LLAMA_LD_LIBRARY_PATH=/opt/llama/lib "
        "dans l'environnement du service (les profils l'honorent).\n"
    )
    assert render_setup_log(event="model-present", value="/models/model.gguf") == "OK:Modèle déjà présent : /models/model.gguf\n"
    assert render_setup_log(event="hf-cli-missing") == (
        "ERROR:Ni 'hf' ni 'huggingface-cli' trouvés — installez : pip install -U huggingface_hub\n"
    )
    assert render_setup_log(event="download-start", value="model.gguf", tier="hf", label="/models") == (
        "INFO:Téléchargement (hf) de model.gguf → /models (peut prendre plusieurs minutes)…\n"
    )
    assert render_setup_log(event="model-downloaded", value="/models/model.gguf") == "OK:Modèle téléchargé : /models/model.gguf\n"
    assert render_setup_log(event="download-failed") == "ERROR:Téléchargement échoué — vérifiez la connectivité / le HF_TOKEN.\n"
    assert render_setup_log(event="download-skipped") == "INFO:Téléchargement ignoré.\n"
    assert render_setup_log(event="tier-activated", tier="48") == "OK:Palier 48 Go activé (alias générique 'arbitrage').\n"
    assert render_setup_log(event="calibration-ok") == "OK:Calibration GPU écrite (placement réel par carte).\n"
    assert render_setup_log(event="calibration-failed") == "WARN:Calibration auto échouée — vérifiez : scripts/check_arbitrage_llm.sh\n"
    assert render_setup_log(event="start-managed") == (
        "INFO:Démarrage de la LLM : géré par TranscrIA via services.arbitrage_script.\n"
    )
    assert render_setup_log(event="switch-incomplete", tier="48") == (
        "WARN:Bascule de palier incomplète — voir scripts/switch_arbitrage_llm.sh 48gb\n"
    )
    assert render_setup_log(event="model-absent") == (
        "INFO:Modèle absent — palier non activé (transcription brute pour l'instant).\n"
    )
    assert render_setup_log(event="ignored") == "INFO:LLM d'arbitrage ignoré — transcription brute. Activable plus tard :\n"
    assert render_setup_log(event="manual-switch") == (
        "INFO:  scripts/switch_arbitrage_llm.sh <palier>  (après téléchargement du modèle)\n"
    )


def test_render_setup_log_rejects_unknown_event():
    with pytest.raises(ValueError, match="événement LLM inconnu : bad"):
        render_setup_log(event="bad")

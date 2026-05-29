"""Tests d'intégrité des scripts de lancement vLLM STT.

Pas de GPU ni de serveur : on valide que les scripts existent, sont
exécutables, ont une syntaxe bash valide, et contiennent les éléments
attendus (binaire vLLM, port, modèle, GPU dédié). Garde-fou contre une
régression de copier-coller entre les trois moteurs.
"""
from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"

_LAUNCHERS = {
    "launch_vllm_cohere.sh": {
        "port": "8001", "model": "CohereLabs/cohere-transcribe-03-2026",
        "served": "cohere-transcribe", "trust_remote": True,
    },
    "launch_vllm_whisper.sh": {
        "port": "8005", "model": "openai/whisper-large-v3",
        "served": "whisper-large-v3", "trust_remote": False,
    },
    "launch_vllm_granite.sh": {
        "port": "8006", "model": "ibm-granite/granite-speech-4.1-2b",
        "served": "granite-speech", "trust_remote": True,
    },
}

_ALL = list(_LAUNCHERS) + ["test_vllm_stt.sh"]


@pytest.mark.parametrize("name", _ALL)
def test_script_existe_et_executable(name):
    path = _SCRIPTS_DIR / name
    assert path.is_file(), f"{name} manquant"
    assert path.stat().st_mode & stat.S_IXUSR, f"{name} non exécutable"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash absent")
@pytest.mark.parametrize("name", _ALL)
def test_syntaxe_bash_valide(name):
    r = subprocess.run(["bash", "-n", str(_SCRIPTS_DIR / name)], capture_output=True, text=True)
    assert r.returncode == 0, f"{name} : syntaxe invalide — {r.stderr}"


@pytest.mark.parametrize("name", _ALL)
def test_robustesse_set_euo_pipefail(name):
    content = (_SCRIPTS_DIR / name).read_text(encoding="utf-8")
    assert "set -euo pipefail" in content, f"{name} sans 'set -euo pipefail'"


@pytest.mark.parametrize("name,spec", _LAUNCHERS.items())
def test_launcher_contenu(name, spec):
    content = (_SCRIPTS_DIR / name).read_text(encoding="utf-8")
    assert "/home/admin_ia/vllm_venv/bin/vllm" in content  # venv vLLM compilé
    assert spec["port"] in content
    assert spec["model"] in content
    assert spec["served"] in content
    assert "CUDA_VISIBLE_DEVICES" in content                 # GPU dédié (assignation statique)
    assert "--gpu-memory-utilization" in content
    if spec["trust_remote"]:
        assert "--trust-remote-code" in content


def test_ports_distincts():
    """Les trois moteurs doivent écouter sur des ports différents (et ≠ 8080 arbitrage, 8002 service)."""
    ports = {spec["port"] for spec in _LAUNCHERS.values()}
    assert len(ports) == len(_LAUNCHERS), "ports en double entre moteurs"
    assert "8080" not in ports, "collision avec la LLM d'arbitrage (8080)"
    assert "8002" not in ports, "collision avec le service d'inférence (8002)"


def test_granite_documente_omni():
    """Granite doit signaler qu'il est un LLM audio-in (pas un ASR à timestamps)."""
    content = (_SCRIPTS_DIR / "launch_vllm_granite.sh").read_text(encoding="utf-8")
    assert "chat/completions" in content
    assert "appoint" in content.lower() or "pas un asr" in content.lower()


def test_arbitrage_reste_sur_llama_cpp():
    """La LLM d'arbitrage ne doit PAS être migrée vers vLLM (reste sur llama.cpp)."""
    arb = _SCRIPTS_DIR / "launch_arbitrage.sh"
    if arb.is_file():
        content = arb.read_text(encoding="utf-8")
        assert "llama-server" in content
        assert "/bin/vllm" not in content

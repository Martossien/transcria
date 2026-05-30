"""Tests d'intégrité des scripts de lancement STT.

Pas de GPU ni de serveur : on valide que les scripts existent, sont exécutables,
ont une syntaxe bash valide, sourcent la bibliothèque commune et contiennent les
éléments attendus (port, modèle, GPU dédié). Garde-fou contre une régression de
copier-coller entre moteurs, et contre un hardcode du serveur d'inférence.
"""
from __future__ import annotations

import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
_LIB = "_stt_serve_lib.sh"

_LAUNCHERS = {
    "launch_stt_cohere.sh": {
        # 8003 et non 8001 : évite que l'EngineCore vLLM (PORT+1) prenne 8002,
        # qui est le port du service inference_service.
        "port": "8003", "model": "CohereLabs/cohere-transcribe-03-2026",
        "served": "cohere-transcribe", "label": "stt-cohere", "trust_remote": True,
    },
    "launch_stt_whisper.sh": {
        "port": "8005", "model": "openai/whisper-large-v3",
        "served": "whisper-large-v3", "label": "stt-whisper", "trust_remote": False,
    },
    "launch_stt_granite.sh": {
        "port": "8007", "model": "ibm-granite/granite-speech-4.1-2b",
        "served": "granite-speech", "label": "stt-granite", "trust_remote": True,
    },
}

# Scripts exécutables (lanceurs + outils). La lib est sourcée, pas exécutée.
_EXECUTABLES = list(_LAUNCHERS) + ["test_stt.sh", "stop_stt.sh"]
_ALL = _EXECUTABLES + [_LIB]


@pytest.mark.parametrize("name", _EXECUTABLES)
def test_script_existe_et_executable(name):
    path = _SCRIPTS_DIR / name
    assert path.is_file(), f"{name} manquant"
    assert path.stat().st_mode & stat.S_IXUSR, f"{name} non exécutable"


def test_lib_existe():
    assert (_SCRIPTS_DIR / _LIB).is_file(), f"{_LIB} manquant"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash absent")
@pytest.mark.parametrize("name", _ALL)
def test_syntaxe_bash_valide(name):
    r = subprocess.run(["bash", "-n", str(_SCRIPTS_DIR / name)], capture_output=True, text=True)
    assert r.returncode == 0, f"{name} : syntaxe invalide — {r.stderr}"


@pytest.mark.parametrize("name", _EXECUTABLES)
def test_robustesse_set_uo_pipefail(name):
    """`-u` + `pipefail` obligatoires. `-e` optionnel : les launchers le retirent
    volontairement (gestion d'erreur explicite avant le `exec`)."""
    content = (_SCRIPTS_DIR / name).read_text(encoding="utf-8")
    assert re.search(r"^set -e?uo pipefail$", content, re.MULTILINE), \
        f"{name} sans 'set -uo pipefail' (ni '-euo')"


def test_lib_contient_invariants_partages():
    """Éléments communs centralisés dans la lib."""
    content = (_SCRIPTS_DIR / _LIB).read_text(encoding="utf-8")
    assert "CUDA_VISIBLE_DEVICES" in content                  # GPU dédié (assignation statique)
    assert "CUDA_HOME" in content                             # JIT kernels (nvcc)
    assert "--gpu-memory-utilization" in content
    assert "stt_serve" in content


def test_lib_moteur_non_hardcode():
    """Le moteur de serving doit être paramétrable (vllm/sglang/custom), pas figé."""
    content = (_SCRIPTS_DIR / _LIB).read_text(encoding="utf-8")
    assert "STT_ENGINE" in content
    assert "sglang.launch_server" in content                  # autre moteur supporté
    assert "STT_SERVE_CMD" in content                          # échappatoire custom
    assert "/home/admin_ia/vllm_venv/bin/vllm" in content      # défaut vllm de la machine


@pytest.mark.parametrize("name,spec", _LAUNCHERS.items())
def test_launcher_contenu(name, spec):
    content = (_SCRIPTS_DIR / name).read_text(encoding="utf-8")
    assert "source" in content and _LIB in content            # mutualise la lib
    assert "stt_serve" in content
    assert spec["port"] in content
    assert spec["model"] in content
    assert spec["served"] in content
    assert spec["label"] in content
    assert "VLLM_" in content                                  # compat ascendante des anciens noms
    if spec["trust_remote"]:
        assert 'STT_TRUST_REMOTE="${STT_TRUST_REMOTE:-1}"' in content
    else:
        assert 'STT_TRUST_REMOTE="${STT_TRUST_REMOTE:-0}"' in content


def test_ports_distincts():
    """Ports HTTP distincts et sans collision (≠ 8002 service, ≠ 8080 arbitrage)."""
    ports = {spec["port"] for spec in _LAUNCHERS.values()}
    assert len(ports) == len(_LAUNCHERS), "ports en double entre moteurs"
    assert "8002" not in ports, "collision avec le service d'inférence (8002)"
    assert "8080" not in ports, "collision avec la LLM d'arbitrage (8080)"
    # Les EngineCore vLLM (PORT+1) ne doivent pas non plus tomber sur 8002.
    assert all(str(int(p) + 1) != "8002" for p in ports), "un EngineCore vLLM (PORT+1) prend 8002"


def test_granite_documente_omni():
    """Granite doit signaler qu'il est un LLM audio-in (pas un ASR à timestamps)."""
    content = (_SCRIPTS_DIR / "launch_stt_granite.sh").read_text(encoding="utf-8")
    assert "chat/completions" in content
    assert "appoint" in content.lower() or "pas un asr" in content.lower()


def test_stop_sans_lsof():
    """stop_stt.sh doit utiliser ss (pas lsof) et accepter une liste de ports."""
    content = (_SCRIPTS_DIR / "stop_stt.sh").read_text(encoding="utf-8")
    assert "lsof" not in content
    assert "ss -tlnp" in content
    assert "STT_STOP_PORTS" in content


def test_arbitrage_reste_sur_llama_cpp():
    """La LLM d'arbitrage ne doit PAS être migrée vers vLLM (reste sur llama.cpp)."""
    arb = _SCRIPTS_DIR / "launch_arbitrage.sh"
    if arb.is_file():
        content = arb.read_text(encoding="utf-8")
        assert "llama-server" in content
        assert "/bin/vllm" not in content

"""Kill patterns GPU uniques — construction et correspondance (vague B3).

``VRAMManager`` et ``GPUAllocator`` construisaient chacun leur copie depuis la
MÊME clé de config (``workflow.scheduling.kill_patterns``) et avaient DIVERGÉ :
le manager protégeait le démon Ollama (``_NEVER_KILL``), l'allocateur non — un
pattern recouvrant « ollama » aurait fait tuer par l'allocateur un démon que le
manager refusait de toucher. Une seule implémentation désormais, avec la
protection unifiée (le sens protecteur l'emporte).
"""
from __future__ import annotations

from collections.abc import Sequence

# Serveurs LLM candidats à la préemption VRAM (surchargables par la config).
DEFAULT_KILL_PATTERNS: tuple[str, ...] = (
    "vllm",
    "llama-server",
    "text-generation-server",
    "aphrodite",
    "sglang",
    "lmdeploy",
    "exllamav2",
)

# Démon(s) persistants qu'on ne SIGKILL JAMAIS, même si un pattern les recouvre :
# tuer le démon Ollama est vain (systemd le relance) et destructeur (il peut servir
# d'autres modèles) — sa VRAM se libère par déchargement HTTP (unload()), pas par kill.
NEVER_KILL: tuple[str, ...] = ("ollama",)


def kill_patterns_from_config(config: dict) -> tuple[str, ...]:
    """Les patterns effectifs (minuscules, vides écartés) depuis
    ``workflow.scheduling.kill_patterns`` — l'UNIQUE lecture de cette clé."""
    scheduling = (config.get("workflow", {}) or {}).get("scheduling", {}) or {}
    raw = scheduling.get("kill_patterns", list(DEFAULT_KILL_PATTERNS))
    return tuple(str(item).lower() for item in raw if str(item).strip())


def matches_kill_pattern(process_name: str, patterns: Sequence[str]) -> bool:
    """Le process est-il préemptable ? (protégés d'abord, patterns ensuite)."""
    lower = process_name.lower()
    if any(protected in lower for protected in NEVER_KILL):
        return False
    return any(pattern in lower for pattern in patterns)

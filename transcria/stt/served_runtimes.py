"""Moteurs STT SERVIS (runtimes C++ audio.cpp / parakeet.cpp) — conventions et présence.

Source UNIQUE de ce que l'installateur, la page Modèles, le formulaire de réglages et le
doctor savent d'un moteur servi : port loopback conventionnel, identifiant de modèle servi,
script de lancement, repli natif, admission VRAM. Ces valeurs vivaient jusqu'ici dans les
en-têtes des scripts et dans ``config.example.yaml`` — un administrateur devait les recopier
à la main (docs/EXTERNAL_STT_RUNTIMES.md). Les poids (dépôt HF, taille, licence) restent dans
``models_catalog._SERVED_STT_SOURCES``.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from transcria.config.local_dirs import resolve_runtimes_dir
from transcria.installer.audiocpp_phase import AUDIOCPP_PINNED_COMMIT, audiocpp_home, audiocpp_is_complete
from transcria.installer.parakeetcpp_phase import (
    PARAKEETCPP_PINNED_COMMIT,
    parakeetcpp_home,
    parakeetcpp_is_complete,
)


@dataclass(frozen=True)
class ServedRuntime:
    name: str
    runtime: str                 # audiocpp | parakeetcpp — phase de l'installateur qui le construit
    port: int                    # port loopback conventionnel (hors llm_cleanup_ports, distincts entre eux)
    model_id: str                # identifiant servi (doit matcher le lanceur)
    script: str                  # lanceur relatif au dépôt
    fallback_backend: str        # repli NATIF si le serveur tombe
    gpu_mem: float               # admission VRAM (fraction de carte) — repères en tête des lanceurs
    label: str
    response_format: str = "json"
    health_path: str = ""        # parakeet-server n'a pas de /v1/models


SERVED_STT: dict[str, ServedRuntime] = {
    "qwen3asr": ServedRuntime(
        name="qwen3asr", runtime="audiocpp", port=8021, model_id="qwen3-asr-1.7b",
        script="scripts/launch_stt_qwen3asr.sh", fallback_backend="whisper", gpu_mem=0.25,
        label="Qwen3-ASR 1.7B (audio.cpp)"),
    "nemotron": ServedRuntime(
        name="nemotron", runtime="parakeetcpp", port=8022, model_id="nemotron",
        script="scripts/launch_stt_nemotron.sh", fallback_backend="parakeet", gpu_mem=0.10,
        label="Nemotron 3.5 ASR 0.6B (parakeet.cpp)", health_path="/health"),
    "voxtralrt": ServedRuntime(
        name="voxtralrt", runtime="audiocpp", port=8024, model_id="voxtral-mini-4b-rt",
        script="scripts/launch_stt_voxtral.sh", fallback_backend="whisper", gpu_mem=0.20,
        label="Voxtral Mini 4B Realtime (audio.cpp)"),
}

#: Commande de provisionnement du runtime (binaire) par famille — affichée quand il manque.
RUNTIME_INSTALL_COMMAND = {
    "audiocpp": "venv/bin/python -m transcria.installer.cli audiocpp --with-model",
    "parakeetcpp": "venv/bin/python -m transcria.installer.cli parakeetcpp",
}


def is_served_backend(name: object) -> bool:
    return isinstance(name, str) and name in SERVED_STT


def runtime_present(name: str, runtimes_dir: Path | None = None) -> bool:
    """Le BINAIRE du runtime est-il construit au commit épinglé ? (Les poids sont une question à
    part — ``models_catalog.served_stt_weights_present`` — téléchargeables après l'activation.)"""
    rt = SERVED_STT.get(name)
    if rt is None:
        return False
    root = runtimes_dir if runtimes_dir is not None else resolve_runtimes_dir()
    if rt.runtime == "audiocpp":
        return audiocpp_is_complete(audiocpp_home(root), AUDIOCPP_PINNED_COMMIT)
    return parakeetcpp_is_complete(parakeetcpp_home(root), PARAKEETCPP_PINNED_COMMIT)


def routed_url(cfg: dict, name: str) -> str:
    """URL de routage déclarée (``inference.stt.backends.<nom>.url``), vide sinon."""
    backends = (((cfg.get("inference") or {}).get("stt") or {}).get("backends") or {})
    spec = backends.get(name) if isinstance(backends, dict) else None
    return str((spec or {}).get("url") or "").strip() if isinstance(spec, dict) else ""


def declared_engine(cfg: dict, name: str) -> dict | None:
    """Entrée ``resource_node.engines`` du moteur (nom nu), ``None`` si absente."""
    for entry in ((cfg.get("resource_node") or {}).get("engines") or []):
        if isinstance(entry, dict) and str(entry.get("name") or "") == name:
            return entry
    return None


def referenced_served_backends(cfg: dict) -> list[str]:
    """Moteurs servis que la configuration UTILISE (pipeline, résumé) — dans l'ordre, sans doublon."""
    models = cfg.get("models") or {}
    names = [models.get("stt_backend"), models.get("summary_stt_backend")]
    return list(dict.fromkeys(str(n) for n in names if is_served_backend(n)))

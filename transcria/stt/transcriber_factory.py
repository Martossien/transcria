"""Façade du registre STT (vague C1) — création, routage distant, VRAM.

La description des backends natifs (builder, VRAM, source HF) vit dans
``stt/registry.py`` (chaque module backend déclare son ``DESCRIPTOR``) ; ici
ne restent que le routage distant (``_should_use_remote_stt``) et le repli
historique « backend inconnu → cohere » (avec avertissement).
"""
import logging

from transcria.config.loader import get_default_config
from transcria.stt import registry
from transcria.stt.base_transcriber import BaseTranscriber
from transcria.stt.remote_transcriber import RemoteTranscriber
from transcria.stt.whisper_transcriber import _effective_whisper_config  # noqa: F401 — ré-exporté (tests historiques)

logger = logging.getLogger(__name__)


def create_transcriber(
    config: dict,
    backend: str | None = None,
    device: str | None = None,
) -> BaseTranscriber:

    if backend is None:
        backend = config.get("models", {}).get("stt_backend", "cohere")

    if _should_use_remote_stt(config, backend):
        logger.info("Transcription : backend distant '%s' (inference.stt)", backend)
        return RemoteTranscriber(config, backend=backend, device=device)

    table = registry.backends()
    descriptor = table.get(backend)
    if descriptor is None:
        logger.warning(
            "Backend STT inconnu '%s', fallback sur cohere. Backends disponibles: %s",
            backend,
            tuple(table),
        )
        descriptor = table["cohere"]

    return descriptor.build(config, device)


# Registre PUBLIC des builders de backends NATIFS (consommé par le fallback local
# de RemoteTranscriber). Un backend SERVI (qwen3asr, nemotron, …) n'y figure pas :
# son repli passe par `inference.stt.backends.<nom>.fallback_backend`.
def local_builders() -> dict:
    return {name: descriptor.build for name, descriptor in registry.backends().items()}


def summary_backend(config: dict) -> str:
    """Backend STT de la PHASE RÉSUMÉ : `models.summary_stt_backend` si défini,
    sinon le backend principal.

    Point de résolution UNIQUE (PISTES_AMELIORATION §2.1) : la phase résumé peut
    utiliser un moteur rapide (ex. kroko, CPU pur → zéro réservation VRAM) sans
    toucher au pipeline principal. Défaut `null` = comportement historique.
    """
    models = config.get("models", {})
    return models.get("summary_stt_backend") or models.get("stt_backend", "cohere")


def _should_use_remote_stt(config: dict, backend: str) -> bool:
    """True si le STT doit passer par un serveur vLLM distant pour ce backend.

    Conditions : `inference.mode` ∈ {remote, hybrid} ET un endpoint est configuré
    pour ce backend dans `inference.stt.backends`. Sinon, transcription locale
    (comportement historique préservé, y compris pour granite/parakeet non mappés).
    """
    inf = config.get("inference", {}) or {}
    if inf.get("mode", "local") not in ("remote", "hybrid"):
        return False
    backends_cfg = ((inf.get("stt", {}) or {}).get("backends", {}) or {})
    return bool((backends_cfg.get(backend, {}) or {}).get("url"))


def list_available_backends() -> list[str]:
    return list(registry.backends())


def get_backend_vram_mb(backend: str, config: dict) -> int:
    # Backend routé vers un serveur distant/servi : la VRAM vit côté serveur (le
    # superviseur/planner l'admissionne) — 0 localement. Sans cette garde, un backend
    # servi inconnu (qwen3asr, nemotron…) retombait sur cohere_vram_mb et réservait
    # 6 Go locaux fantômes.
    if _should_use_remote_stt(config, backend):
        return 0
    descriptor = registry.backends().get(backend)
    if descriptor is not None:
        return descriptor.vram_mb(config)
    # Repli historique : backend inconnu non routé → empreinte cohere.
    return int(config.get("gpu", {}).get("cohere_vram_mb", get_default_config()["gpu"]["cohere_vram_mb"]))

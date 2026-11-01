"""Disponibilité de la LLM d'arbitrage avant une phase LLM — règle de panne UNIQUE.

Partagé par les phases correction, résumé, relecture finale (+ champs de type), affinage
et arbitrage multi-STT, pour qu'aucune ne recopie sa variante.

- **Local / distant** : un seul appel à ``ensure_arbitrage_llm_ready`` — comportement
  historique inchangé (CAS A/B/C en local ; ``vram_wait`` décidé par la phase en distant).
- **Cloud** (``services.backend: cloud``, docs/LLM_CLOUD.md § 3.1.7) : en échec, UNE seule
  reprise automatique après une courte pause, puis échec explicite et relançable. JAMAIS
  ``vram_wait`` : une panne du fournisseur ou une clé révoquée n'est pas une pénurie de VRAM
  (re-file sans fin + e-mail admin « attente de VRAM » trompeur).
"""
from __future__ import annotations

import logging
import time
from typing import Any

from transcria.config.views import LlmCloudView

# Ré-exportés : les phases LLM lisent le mode et le libellé d'endpoint ICI (une seule
# dépendance « LLM d'arbitrage » par phase — ratchet de fan-out).
from transcria.gpu.arbitrage_endpoint import (
    describe_arbitrage_endpoint,
    is_cloud_backend,
    is_remote_arbitrage,
)

__all__ = [
    "CLOUD_RETRY_DELAY_S",
    "cloud_provider",
    "cloud_unavailable_error",
    "describe_arbitrage_endpoint",
    "ensure_llm_ready",
    "is_cloud_backend",
    "is_remote_arbitrage",
    "llm_running_before_phase",
]

logger = logging.getLogger(__name__)

# Pause avant l'unique reprise automatique en cloud (coupure réseau brève, 5xx passager).
CLOUD_RETRY_DELAY_S = 5.0

# Couture de test : les tests remplacent cette pause (jamais de ``time.sleep`` réel en CI).
_pause = time.sleep


def cloud_provider(config: dict) -> str:
    """Identifiant du fournisseur cloud configuré (``?`` s'il est indéterminable)."""
    return LlmCloudView.from_config(config).provider or "?"


def cloud_unavailable_error(config: dict) -> str:
    """Message d'échec lisible (FR) d'une phase LLM quand le fournisseur reste injoignable."""
    return (
        f"LLM cloud {cloud_provider(config)} injoignable — vérifier la clé d'API, le réseau "
        "et l'état du fournisseur (détail dans le journal) ; traitement relançable"
    )


def ensure_llm_ready(vram: Any, config: dict, expected_model_id: str | None = None) -> bool:
    """S'assure que la LLM d'arbitrage répond, selon la règle de panne du mode configuré.

    Args:
        vram: le ``VRAMManager`` du runner (ou une doublure exposant
            ``ensure_arbitrage_llm_ready``).
        config: configuration effective du traitement.
        expected_model_id: alias attendu (ignoré en cloud par ``VRAMManager``).

    Returns:
        True si la LLM est prête. En cloud, False seulement après l'unique reprise.
    """
    if vram.ensure_arbitrage_llm_ready(expected_model_id=expected_model_id):
        return True
    if not is_cloud_backend(config):
        return False
    provider = cloud_provider(config)
    logger.warning(
        "LLM cloud %s injoignable — reprise automatique unique dans %.0f s",
        provider, CLOUD_RETRY_DELAY_S,
    )
    _pause(CLOUD_RETRY_DELAY_S)
    if vram.ensure_arbitrage_llm_ready(expected_model_id=expected_model_id):
        logger.info("LLM cloud %s joignable à la reprise automatique", provider)
        return True
    logger.error(
        "LLM cloud %s toujours injoignable après la reprise automatique — échec explicite "
        "(relançable, aucune mise en attente de VRAM)", provider,
    )
    return False


def llm_running_before_phase(vram: Any, config: dict) -> bool:
    """État « LLM déjà active » avant la phase (décide de l'arrêt après une exception).

    En cloud, rien n'est lancé localement par la phase : on répond True SANS sonder
    (chaque sonde serait une requête vers le fournisseur, redondante avec ``ensure``).
    Ailleurs : la sonde historique ``is_arbitrage_llm_running``.
    """
    if is_cloud_backend(config):
        return True
    return bool(vram.is_arbitrage_llm_running())

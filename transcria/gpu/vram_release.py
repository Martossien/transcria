"""Libération de VRAM SOUS PRESSION — registre de relâcheurs opportunistes.

Constat (tests réels réunions, 2026-07-30) : le transcripteur de la FAÇADE live reste
résident après une réunion (déchargé au bout de `idle_unload_s`) — pendant ce délai, la
LLM d'arbitrage ne peut pas se (re)placer et les jobs tournent en « attente de VRAM »
toutes les 30 s. Plutôt que raccourcir la minuterie (rechargements pendant les réunions),
l'ALLOCATEUR demande la libération au moment précis où il en a besoin.

Couches respectées : ce module vit dans `gpu/` (aucun import du web) ; la façade
S'ENREGISTRE ici à son chargement. Les relâcheurs sont best-effort et idempotents.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

_releasers: list[Callable[[], None]] = []


def register_releaser(fn: Callable[[], None]) -> None:
    if fn not in _releasers:
        _releasers.append(fn)


def release_idle_vram() -> None:
    """Appelle chaque relâcheur enregistré — jamais d'exception vers l'appelant."""
    for fn in list(_releasers):
        try:
            fn()
        except Exception:  # noqa: BLE001
            logger.debug("relâcheur VRAM en échec", exc_info=True)

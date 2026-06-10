"""Récupération de VRAM par arrêt de NOS propres process GPU gérés et inactifs.

Catégorie 1 de la politique de préemption : on n'arrête que ce qui nous appartient
(LLM d'arbitrage trackée, arrêtée proprement via son script) et seulement quand
personne ne l'utilise (verrou LLM libre). Jamais un process tiers — c'est le rôle,
opt-in et gaté par le calendrier, de la catégorie « aggressive » (`force_free_gpu`).

Partagé entre `WorkflowRunner` (récupération en cours de phase, sur GPUSessionError)
et `QueueScheduler` (récupération à l'admission, avant dispatch). Voir
docs/SERVICE_RESSOURCES_GPU.md §7.2-bis.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class _LLMLock(Protocol):
    def try_acquire_llm(self, job_id: str = ..., timeout_s: float = ...) -> bool: ...
    def release_llm(self, job_id: str | None = ...) -> None: ...


def stop_idle_arbitrage_llm(allocator: _LLMLock, vram: Any, *, log: Any = logger) -> bool:
    """Arrête la LLM d'arbitrage si elle tourne ET qu'aucun job ne l'utilise.

    Le verrou LLM libre prouve qu'aucune phase (résumé/correction/relecture) ne s'en
    sert : on peut l'arrêter sans casser un job en cours (garde-fou multi-job). C'est
    notre process géré (PID tracké, arrêt propre) — relancé à la demande par la phase
    LLM suivante. Retourne True si on l'a stoppée (VRAM potentiellement libérée).

    Best-effort : ne lève jamais.
    """
    try:
        if not vram.is_arbitrage_llm_running():
            return False
        # try_acquire_llm(timeout_s=0) ne réussit que si le verrou est libre.
        if not allocator.try_acquire_llm("", timeout_s=0):
            log.info("VRAM bloquée par la LLM d'arbitrage, mais elle est en cours d'utilisation — on patiente")
            return False
        try:
            log.warning("Arrêt de la LLM d'arbitrage inactive pour libérer la VRAM")
            vram.stop_arbitrage_llm()
        finally:
            allocator.release_llm("")
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort, ne jamais aggraver
        log.warning("Récupération VRAM via arrêt LLM d'arbitrage impossible: %s", exc)
        return False

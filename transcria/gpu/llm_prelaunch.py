"""Pré-lancement opportuniste de la LLM d'arbitrage (PISTES_AMELIORATION lot 2, §4.3-4).

Appelé par le wizard à la fin de l'étape ANALYSE (opt-in
``workflow.arbitration_llm.prelaunch_at_analyze``) : les secondes de démarrage
(17 s llama.cpp, minutes en vLLM) s'absorbent pendant que l'utilisateur remplit
les étapes suivantes. Déclenché à l'analyse et non à l'upload : un utilisateur qui
a franchi l'analyse est engagé dans le parcours (moins de GPU dépensé sur les
abandons précoces).

Best-effort en thread — n'affecte jamais la réponse HTTP. Discipline B3 : jamais
de lancement sans détenir le verrou LLM ; jamais de préemption VRAM.
"""
from __future__ import annotations

import logging
import threading

from transcria.config.views import GpuView
from transcria.gpu.opencode_setup import is_remote_arbitrage
from transcria.gpu.vram_manager import VRAMManager
from transcria.queue.allocator import GPUAllocator

logger = logging.getLogger(__name__)

_PRELAUNCH_OWNER = "__prelaunch__"


def maybe_prelaunch_arbitrage_llm(cfg: dict) -> None:
    """Pré-lance la LLM d'arbitrage en tâche de fond si l'opt-in l'autorise."""
    llm_cfg = cfg.get("workflow", {}).get("arbitration_llm", {}) or {}
    if not llm_cfg.get("prelaunch_at_analyze", False):
        return
    if llm_cfg.get("enabled") is False or is_remote_arbitrage(cfg):
        return  # coupée explicitement, ou distante (rien à lancer localement)

    threading.Thread(target=lambda: _prelaunch(cfg), name="llm-prelaunch", daemon=True).start()


def _prelaunch(cfg: dict) -> None:
    try:
        allocator = GPUAllocator.get_instance(cfg)
        # Même discipline que l'arrêt de fin de pipeline (course B3) : ne JAMAIS
        # lancer sans détenir le verrou LLM. Verrou occupé → un job s'en sert ou
        # l'arrête : dans les deux cas le pré-lancement n'a plus d'objet.
        if not allocator.try_acquire_llm(_PRELAUNCH_OWNER):
            return
        try:
            vram = VRAMManager(cfg)
            if vram.is_arbitrage_llm_running():
                return  # déjà chaude (CAS A) — le but est atteint
            if not allocator.can_host_llm(GpuView.from_config(cfg).llm_vram_mb):
                return  # VRAM occupée : jamais de préemption pour un pré-lancement
            logger.info("[wizard] Pré-lancement LLM arbitrage (étape analyse)")
            vram.launch_arbitrage_llm()
        finally:
            allocator.release_llm(_PRELAUNCH_OWNER)
    except Exception:  # noqa: BLE001 — opportuniste : l'échec du pré-lancement est sans effet
        logger.debug("[wizard] Pré-lancement LLM arbitrage abandonné", exc_info=True)

"""Outillage LLM — orchestration opencode, parsing de réponses, politique de langue,
qualification du binaire llama.cpp.

Né du rangement de l'audit du 2026-07-30 : ces modules vivaient dans ``transcria/gpu/``
(~34 % du paquet) sans rien devoir au GPU. La frontière : ``gpu/`` gère des CARTES
(placement, VRAM, cycle de vie des serveurs) ; ``llm_tools/`` gère du TEXTE et des
OUTILS (CLI opencode, prompts, parsing). ``llm_tools`` peut importer ``gpu`` (résolution
d'endpoint) — jamais l'inverse.
"""

from transcria.llm_tools.opencode_runner import OpenCodeRunner  # noqa: E402 — façade

__all__ = ["OpenCodeRunner"]

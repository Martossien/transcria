"""Provenance d'un segment de transcription — niveau de confiance / origine.

Couture 1 du chantier temps réel (docs/TEMPS_REEL_REUNIONS.md). Additive et
opt-in par nature : le pipeline batch produit TOUJOURS ``canonical`` ; la chaîne
live (à venir) posera ``partial`` → ``provisional`` → ``final_live``, puis le
pipeline offline remplacera ``final_live`` par ``canonical`` en fin de réunion
(« le direct suit, le pipeline produit la référence »).

Poser le champ dès maintenant rend le live *appelable* sans toucher au modèle :
il ne fera que remplir les autres états. Le champ transite tel quel dans
``metadata/transcription_segments.json`` (dump brut) et est ignoré par le SRT
(``segments_to_srt`` ne lit que start/end/text/speaker) → zéro régression golden.
"""
from __future__ import annotations

# Ordre = confiance croissante ; le pipeline remplace final_live par canonical.
PARTIAL = "partial"          # texte live instable (peut changer au prochain paquet)
PROVISIONAL = "provisional"  # segment live stabilisé (p. ex. local-agreement)
FINAL_LIVE = "final_live"    # segment final du moteur temps réel (fin de tour)
CANONICAL = "canonical"      # recalculé/validé par le pipeline TranscrIA (référence)

PROVENANCES: tuple[str, ...] = (PARTIAL, PROVISIONAL, FINAL_LIVE, CANONICAL)


def stamp_provenance(segments: list[dict], provenance: str = CANONICAL) -> list[dict]:
    """Tague chaque segment d'une ``provenance`` s'il n'en a pas déjà une.

    Additif et **idempotent** : ne touche jamais un segment déjà taggué (le live
    aura posé sa propre valeur). Mutation en place + retour pour chaînage.
    """
    for seg in segments:
        if isinstance(seg, dict):
            seg.setdefault("provenance", provenance)
    return segments

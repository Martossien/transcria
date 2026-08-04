"""Étage A de la politique anti-hallucination : suppression CORROBORÉE.

Principe (arbitré 2026-08-04) : on n'est jamais certain sur un signal isolé — on le
devient quand deux signaux INDÉPENDANTS convergent. Un segment n'est supprimé que si :

1. son texte matche une signature du catalogue avec ``action: delete``
   (posée par ``SegmentReliabilityScorer`` dans ``hallucination_signature``), **ET**
2. l'acoustique confirme qu'il n'y a pas de parole dessous : ``no_speech_prob`` élevé,
   ou recouvrement majoritaire avec des zones non-parole de la scène audio
   (``noEnergy``/``music``/``noise``).

Signature sans corroboration → le segment RESTE (rétrogradé en signalement : la passe
de correction le voit dans les hints). Toute suppression est tracée — texte, raison,
preuves — pour le rapport qualité : jamais silencieuse, toujours récupérable.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Recouvrement minimal du segment par des zones non-parole pour valoir corroboration.
_NON_SPEECH_OVERLAP_MIN = 0.5
_NON_SPEECH_LABELS = frozenset({"noEnergy", "music", "noise"})
# Seuil no_speech_prob de corroboration — volontairement PLUS EXIGEANT que le seuil
# de simple signalement du scorer (0.5) : ici on supprime, pas on signale.
_NO_SPEECH_PROB_DELETE = 0.8


def deletion_enabled(config: dict) -> bool:
    cfg = (config.get("workflow", {}) or {}).get("segment_reliability", {}) or {}
    return bool(cfg.get("delete_confirmed_hallucinations", True))


def _non_speech_overlap_ratio(segment: dict, scene_segments: list[dict]) -> float:
    start = float(segment.get("start") or 0.0)
    end = float(segment.get("end") or 0.0)
    duration = end - start
    if duration <= 0:
        return 0.0
    covered = 0.0
    for zone in scene_segments:
        if not isinstance(zone, dict) or zone.get("label") not in _NON_SPEECH_LABELS:
            continue
        overlap = min(end, float(zone.get("end") or 0.0)) - max(start, float(zone.get("start") or 0.0))
        if overlap > 0:
            covered += overlap
    return min(1.0, covered / duration)


def _corroboration(segment: dict, scene_segments: list[dict]) -> str | None:
    """La preuve acoustique retenue, ou ``None`` si rien ne corrobore."""
    nsp = segment.get("no_speech_prob")
    if nsp is not None and float(nsp) >= _NO_SPEECH_PROB_DELETE:
        return f"no_speech_prob={float(nsp):.2f}"
    ratio = _non_speech_overlap_ratio(segment, scene_segments)
    if ratio >= _NON_SPEECH_OVERLAP_MIN:
        return f"recouvrement_non_parole={ratio:.0%}"
    return None


def apply_deletion_policy(
    segments: list[dict],
    *,
    scene: dict | None,
    config: dict,
) -> tuple[list[dict], list[dict]]:
    """Retourne ``(segments_conservés, segments_supprimés_tracés)``.

    Chaque suppression emporte son dossier de preuves (texte, pattern, corroboration).
    Sans corroboration, la signature ``delete`` est rétrogradée : le segment reste,
    ses raisons de fiabilité aussi — la correction LLM le verra dans les hints.
    """
    if not deletion_enabled(config):
        return segments, []
    scene_segments = (scene or {}).get("scene_segments") or []
    kept: list[dict] = []
    removed: list[dict] = []
    for segment in segments:
        signature = segment.get("hallucination_signature") or {}
        if signature.get("action") == "delete":
            evidence = _corroboration(segment, scene_segments)
            if evidence is not None:
                removed.append({
                    "start": segment.get("start"),
                    "end": segment.get("end"),
                    "speaker": segment.get("speaker", ""),
                    "text": str(segment.get("text") or ""),
                    "pattern": signature.get("pattern"),
                    "signature_source": signature.get("source"),
                    "corroboration": evidence,
                })
                continue
        kept.append(segment)
    if removed:
        logger.info(
            "Hallucinations supprimées (signature + corroboration acoustique) : %d segment(s)",
            len(removed))
    return kept, removed

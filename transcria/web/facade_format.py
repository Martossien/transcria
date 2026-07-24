"""Sérialisation OpenAI-audio de la façade STT (Phase K — temps réel).

Fonctions PURES (zéro dépendance Flask) : elles transforment les segments internes
TranscrIA (dicts ``start``/``end``/``text``/``speaker``/``words``/``provenance``…)
vers les formats de réponse de l'API *OpenAI Audio Transcriptions*. Le format
``srt`` reste délégué à ``BaseTranscriber.segments_to_srt`` (déjà éprouvé) côté
route — pas de duplication de la logique de sous-titrage ici.

Testables sans app Flask ni moteur STT : voir tests/test_facade_format.py.
"""
from __future__ import annotations

#: Formats de sortie acceptés par ``POST /v1/audio/transcriptions`` (OpenAI-audio).
RESPONSE_FORMATS = ("json", "verbose_json", "text", "srt")
DEFAULT_RESPONSE_FORMAT = "json"

#: Champs internes PRÉSERVÉS dans les segments de ``verbose_json`` : un client
#: OpenAI standard les ignore, un client TranscrIA les exploite (identité du
#: locuteur, provenance live/canonical, niveau de confiance, mots horodatés).
_PRESERVED_SEGMENT_FIELDS = ("speaker", "provenance", "reliability", "words")


def full_text(segments: list[dict]) -> str:
    """Texte complet = concaténation des segments non vides, séparés par une espace."""
    return " ".join(
        stripped
        for seg in segments
        if (stripped := (seg.get("text") or "").strip())
    ).strip()


def _duration(segments: list[dict]) -> float:
    """Durée = fin du dernier segment horodaté (0.0 si aucun timestamp)."""
    ends = [float(seg["end"]) for seg in segments if seg.get("end") is not None]
    return round(max(ends), 3) if ends else 0.0


def simple_json(segments: list[dict]) -> dict:
    """Format ``json`` OpenAI : uniquement le texte agrégé."""
    return {"text": full_text(segments)}


def verbose_json(segments: list[dict], language: str) -> dict:
    """Format ``verbose_json`` OpenAI : texte + métadonnées + segments enrichis."""
    out_segments = []
    for idx, seg in enumerate(segments):
        entry: dict = {
            "id": idx,
            "start": seg.get("start"),
            "end": seg.get("end"),
            "text": (seg.get("text") or "").strip(),
        }
        for field in _PRESERVED_SEGMENT_FIELDS:
            if field in seg:
                entry[field] = seg[field]
        out_segments.append(entry)
    return {
        "task": "transcribe",
        "language": language,
        "duration": _duration(segments),
        "text": full_text(segments),
        "segments": out_segments,
    }

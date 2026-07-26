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


def _srt_time(seconds: object) -> str:
    """Horodatage SRT ``HH:MM:SS,mmm``."""
    value = float(seconds) if isinstance(seconds, (int, float)) else 0.0
    total_ms = max(int(round(value * 1000)), 0)
    ms = total_ms % 1000
    total_s = total_ms // 1000
    return f"{total_s // 3600:02d}:{(total_s % 3600) // 60:02d}:{total_s % 60:02d},{ms:03d}"


def segments_to_srt(segments: list[dict]) -> str:
    """Rendu SRT PUR depuis des segments dict.

    `BaseTranscriber.segments_to_srt` exige un objet transcriber : indisponible quand la
    façade DÉLÈGUE l'inférence au nœud de calcul (elle ne reçoit alors que des segments).
    Même sortie, sans dépendance à un moteur.
    """
    lines: list[str] = []
    index = 0
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        index += 1
        speaker = (seg.get("speaker") or "").strip()
        lines.append(str(index))
        lines.append(f"{_srt_time(seg.get('start'))} --> {_srt_time(seg.get('end'))}")
        lines.append(f"{speaker}: {text}" if speaker else text)
        lines.append("")
    return "\n".join(lines)

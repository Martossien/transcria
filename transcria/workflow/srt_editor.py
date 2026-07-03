"""Éditeur de transcription intégré — modèle serveur (lot A).

Cf. docs/EDITEUR_SRT_INTEGRE.md §3.1. Module PUR (aucune dépendance Flask/DB) :

- ``parse_srt_chunks``   : SRT → chunks ``{index, start_ms, end_ms, speaker_id,
  speaker_name, text}``. Le locuteur est un PRÉFIXE TEXTUEL (`SPEAKER_01(Nom): …`),
  parsé avec tolérance (sans nom, sans préfixe) — jamais d'échec sur un SRT lisible.
- ``serialize_chunks``   : chunks → SRT canonique (renumérotation séquentielle,
  fichier terminé par un saut de ligne). **Round-trip à l'octet près** sur un SRT non
  modifié, À UNE normalisation près : un fichier SANS saut de ligne final en gagne un
  (constaté sur les SRT réels : ceux écrits par la correction LLM n'en ont pas). Test
  d'or du module — l'éditeur n'altère JAMAIS ce que l'utilisateur n'a pas touché.
- ``validate_chunks``    : avertissements NON bloquants (D6 : les chevauchements
  existent dans les vraies données et l'utilisateur a toujours raison).
- ``compute_speaker_stats`` : recalcul de ``speakers/speaker_stats.json`` depuis les
  chunks édités (A2 : le tableau des participants du DOCX lit ces stats — elles
  doivent suivre les réattributions faites dans l'éditeur).
"""
from __future__ import annotations

import re

# Préfixe locuteur observé dans les SRT réels : `SPEAKER_01(Vendeur / fromager): texte`
# ou `SPEAKER_01: texte`. Le nom entre parenthèses peut contenir tout sauf une
# parenthèse fermante finale ; l'espace après `:` est optionnel.
_SPEAKER_PREFIX = re.compile(r"^(?P<id>SPEAKER_\d+)(?:\((?P<name>[^)]*)\))?:\s?(?P<rest>.*)$", re.DOTALL)

_TIMESTAMP = re.compile(
    r"^(?P<h1>\d{2}):(?P<m1>\d{2}):(?P<s1>\d{2}),(?P<ms1>\d{3})"
    r" --> "
    r"(?P<h2>\d{2}):(?P<m2>\d{2}):(?P<s2>\d{2}),(?P<ms2>\d{3})\s*$"
)


class SrtParseError(ValueError):
    """SRT illisible (structure irrécupérable) — jamais levée pour un simple écart."""


def _to_ms(h: str, m: str, s: str, ms: str) -> int:
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms)


def _fmt_ms(total_ms: int) -> str:
    total_ms = max(0, int(total_ms))
    h, rest = divmod(total_ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def split_speaker_prefix(text: str) -> tuple[str | None, str | None, str]:
    """``SPEAKER_01(Nom): reste`` → ``("SPEAKER_01", "Nom", "reste")`` ; tolérant."""
    match = _SPEAKER_PREFIX.match(text)
    if not match:
        return None, None, text
    name = match.group("name")
    return match.group("id"), (name if name else None), match.group("rest")


def join_speaker_prefix(speaker_id: str | None, speaker_name: str | None, text: str) -> str:
    if not speaker_id:
        return text
    if speaker_name:
        return f"{speaker_id}({speaker_name}): {text}"
    return f"{speaker_id}: {text}"


def parse_srt_chunks(srt_text: str) -> list[dict]:
    """SRT → liste de chunks. Blocs malformés ISOLÉS ignorés (comptés nulle part :
    l'éditeur montre ce qui est lisible) ; un fichier sans AUCUN bloc lisible mais
    non vide lève :class:`SrtParseError`."""
    chunks: list[dict] = []
    if not srt_text or not srt_text.strip():
        return chunks

    blocks = re.split(r"\n\s*\n", srt_text.strip())
    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 2:
            continue
        # Ligne 1 = index (tolérant : absente si le bloc commence par le timestamp)
        offset = 1 if lines[0].strip().isdigit() else 0
        if len(lines) <= offset:
            continue
        ts = _TIMESTAMP.match(lines[offset].strip())
        if not ts:
            continue
        text = "\n".join(lines[offset + 1:])
        speaker_id, speaker_name, rest = split_speaker_prefix(text)
        chunks.append({
            "start_ms": _to_ms(ts["h1"], ts["m1"], ts["s1"], ts["ms1"]),
            "end_ms": _to_ms(ts["h2"], ts["m2"], ts["s2"], ts["ms2"]),
            "speaker_id": speaker_id,
            "speaker_name": speaker_name,
            "text": rest,
        })
    if not chunks:
        raise SrtParseError("aucun bloc SRT lisible")
    return chunks


def serialize_chunks(chunks: list[dict]) -> str:
    """Chunks → SRT canonique (renumérotation séquentielle, LF, fichier terminé par
    une ligne vide — le format produit par le pipeline)."""
    blocks: list[str] = []
    for i, chunk in enumerate(chunks, start=1):
        text = join_speaker_prefix(chunk.get("speaker_id"), chunk.get("speaker_name"),
                                   str(chunk.get("text") or ""))
        blocks.append(
            f"{i}\n{_fmt_ms(chunk['start_ms'])} --> {_fmt_ms(chunk['end_ms'])}\n{text}"
        )
    return "\n\n".join(blocks) + "\n" if blocks else ""


def validate_chunks(chunks: list[dict], *, audio_duration_ms: int | None = None) -> list[str]:
    """Avertissements NON bloquants (l'utilisateur garde toujours la main — D6)."""
    warnings: list[str] = []
    overlaps = 0
    for i, chunk in enumerate(chunks):
        if chunk["end_ms"] <= chunk["start_ms"]:
            warnings.append(f"Segment {i + 1} : durée nulle ou négative.")
        if not str(chunk.get("text") or "").strip():
            warnings.append(f"Segment {i + 1} : texte vide.")
        if i and chunk["start_ms"] < chunks[i - 1]["end_ms"]:
            overlaps += 1
    if overlaps:
        warnings.append(f"Chevauchements : {overlaps} segment(s) commencent avant la fin du précédent.")
    if audio_duration_ms and chunks and chunks[-1]["end_ms"] > audio_duration_ms + 2000:
        warnings.append("Le dernier segment dépasse la durée de l'audio.")
    return warnings


def compute_speaker_stats(chunks: list[dict]) -> dict:
    """Recalcule ``speakers/speaker_stats.json`` depuis les chunks (A2).

    Après édition, les stats reflètent le TEMPS DE PAROLE TRANSCRIT (Σ durées des
    chunks par locuteur) — c'est ce que le lecteur du Word doit voir. Format
    identique au fichier produit par la diarisation (clés consommées par
    ``docx_report`` : ``speaker_id``, ``speaking_time_seconds``, ``turn_count``).
    """
    per_speaker: dict[str, dict] = {}
    for chunk in chunks:
        speaker_id = chunk.get("speaker_id") or "—"
        entry = per_speaker.setdefault(speaker_id, {
            "speaker_id": speaker_id,
            "speaking_time_seconds": 0.0,
            "turn_count": 0,
        })
        entry["speaking_time_seconds"] += max(0, chunk["end_ms"] - chunk["start_ms"]) / 1000.0
        entry["turn_count"] += 1
    speakers = sorted(per_speaker.values(), key=lambda s: -s["speaking_time_seconds"])
    for entry in speakers:
        entry["speaking_time_seconds"] = round(entry["speaking_time_seconds"], 3)
    return {"speakers": speakers, "source": "srt_editor"}

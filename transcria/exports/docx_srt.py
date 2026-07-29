"""Parsing SRT pour l'export DOCX — blocs, locuteurs, durée. Pur texte, sans python-docx.

Extrait de docx_report.py (vague 0, 2026-07). La façade docx_report ré-exporte.
"""
from __future__ import annotations

import re

_SRT_BLOCK = re.compile(
    r"\d+\s*\n"
    r"(\d{2}:\d{2}:\d{2}),\d{3}\s*-->\s*\d{2}:\d{2}:\d{2},\d{3}\s*\n"
    r"(.*?)(?=\n\n|\Z)",
    re.DOTALL,
)

_SPEAKER_LINE = re.compile(r"^[A-Z_0-9]+\(([^)]+)\):\s*(.+)$")
# Repli : SRT dont les locuteurs sont au format lisible « Nom: texte » (observé en réel —
# un agent LLM peut réécrire le préfixe `SPEAKER_XX(Nom):` en `Nom:` malgré la consigne).
# Sans ce repli, la colonne Locuteur du rapport est vide et le nom reste collé au texte.
# Heuristique prudente : majuscule initiale (accents inclus), ≤ 40 caractères avant le
# deux-points — un libellé de prose type « Note : » peut matcher, compromis assumé.

_SPEAKER_LINE_PLAIN = re.compile(r"^([A-ZÀ-ÖØ-Þ][^:\n]{0,39}?)\s*:\s+(.+)$")

_SRT_END_TIME = re.compile(r"-->\s*(\d{2}):(\d{2}):(\d{2}),\d{3}")

def _parse_srt(srt_text: str) -> list[dict[str, str]]:
    """Retourne une liste de {"timestamp": "HH:MM:SS", "speaker": str, "text": str}."""
    entries: list[dict[str, str]] = []
    for m in _SRT_BLOCK.finditer(srt_text.strip()):
        timestamp = m.group(1)
        body = m.group(2).strip()
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            sm = _SPEAKER_LINE.match(line) or _SPEAKER_LINE_PLAIN.match(line)
            if sm:
                entries.append({
                    "timestamp": timestamp,
                    "speaker": sm.group(1),
                    "text": sm.group(2),
                })
            else:
                entries.append({"timestamp": timestamp, "speaker": "", "text": line})
    return entries

def _srt_duration_seconds(srt_text: str) -> int:
    """Durée de la réunion = dernier timestamp de FIN du SRT (0 si introuvable)."""
    last = 0
    for h, m, s in _SRT_END_TIME.findall(srt_text):
        last = max(last, int(h) * 3600 + int(m) * 60 + int(s))
    return last

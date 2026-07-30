"""`live/captions.jsonl` — suivi en direct PROVISOIRE d'une réunion (vague 5, lot C, D5.5).

Le direct PRÉCÈDE le canonical et ne le remplace jamais (ADR-001, décision D5) : ce fichier
est une TRACE plafonnée que la page du job affiche pendant la réunion — le pipeline batch
produit ensuite la référence, et le panneau s'efface. Plafond par troncature de TÊTE,
annoncée dans le flux (jamais silencieuse) via un marqueur `{"truncated": N}` en première
ligne. Chaque tour porte un numéro `n` MONOTONE (survit à la troncature) : le poll de la
page relit en delta (`after=<n>`), jamais tout le fichier ré-affiché.

PUR (chemin injecté, aucune dépendance Flask) — testé sans serveur.
"""
from __future__ import annotations

import json
from pathlib import Path

DEFAULT_MAX_CAPTION_LINES = 2000
_MAX_TEXT_CHARS = 500
_MAX_SPEAKER_CHARS = 120


def sanitize_caption(raw) -> dict | None:
    """Un tour candidat → enregistrement sûr, ou None (jamais une exception) : le runner
    relaie ce que le bot a émis, le serveur reste le juge de ce qui entre dans le fichier."""
    if not isinstance(raw, dict):
        return None
    text = str(raw.get("text") or "").strip()
    if not text:
        return None
    try:
        start = round(float(raw.get("start") or 0.0), 3)
        end = round(float(raw.get("end") or 0.0), 3)
    except (TypeError, ValueError):
        return None
    return {"start": max(start, 0.0), "end": max(end, 0.0),
            "speaker": str(raw.get("speaker") or "").strip()[:_MAX_SPEAKER_CHARS],
            "text": text[:_MAX_TEXT_CHARS]}


def _load_lines(path: Path) -> tuple[int, list[dict]]:
    """(total déjà retiré, enregistrements présents) — un fichier corrompu repart de zéro
    (le direct est provisoire, jamais une raison d'échouer)."""
    truncated = 0
    records: list[dict] = []
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0, []
    for line in raw_lines:
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        if "truncated" in payload:
            truncated = int(payload.get("truncated") or 0)
        elif payload.get("text"):
            records.append(payload)
    return truncated, records


def append_captions(path: Path, captions: list[dict], *,
                    max_lines: int = DEFAULT_MAX_CAPTION_LINES) -> int:
    """Ajoute des tours (déjà passés par `sanitize_caption`) en maintenant plafond,
    numérotation monotone et marqueur de troncature. Retourne le nombre ajouté."""
    if not captions:
        return 0
    truncated, records = _load_lines(path)
    next_n = (records[-1].get("n", 0) + 1) if records else truncated + 1
    for caption in captions:
        records.append({"n": next_n, **caption})
        next_n += 1
    dropped = max(len(records) - max(int(max_lines), 1), 0)
    if dropped:
        truncated += dropped
        records = records[dropped:]
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ([json.dumps({"truncated": truncated}, ensure_ascii=False)] if truncated else []) \
        + [json.dumps(r, ensure_ascii=False) for r in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(captions)


def read_captions(path: Path, after: int = 0) -> tuple[list[dict], int, int]:
    """(tours de numéro > `after`, curseur pour le prochain poll, total retiré au plafond).
    Fichier absent = réunion sans tour encore : ([], after, 0)."""
    if not path.is_file():
        return [], after, 0
    truncated, records = _load_lines(path)
    fresh = [r for r in records if int(r.get("n") or 0) > after]
    next_cursor = int(records[-1].get("n") or after) if records else after
    return fresh, max(next_cursor, after), truncated

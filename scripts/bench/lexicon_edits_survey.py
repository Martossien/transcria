#!/usr/bin/env python3
"""Relevé des corrections HUMAINES et du devenir des termes douteux sur les jobs réels d'une
instance — COMPTAGES SEULS, jamais un terme, un nom ni une phrase (les fichiers de jobs sont
des données de réunion : rien n'en sort).

Mesure 2026-11-02 sur une machine de TEST (65 jobs) : 497 termes douteux « A / B » sur 1319,
789 variantes « non résolues » dont 84 % venaient de là ou de mentions comptées comme fautes →
`llm_parsing.normalize_suspect_terms`. À rejouer sur une instance d'USAGE RÉEL pour confirmer.

Usage (sur la machine, avec le python du venv ; les fichiers de jobs appartiennent souvent au
compte de service → sudo) :

    sudo venv/bin/python -I scripts/bench/lexicon_edits_survey.py --jobs-dir jobs \\
        --database-url "$TRANSCRIA_DATABASE_URL"

Sans ``--database-url``, tous les dossiers de ``--jobs-dir`` sont parcourus (jobs de test compris).
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from transcria.llm_tools.llm_parsing import ALTERNATIVES_RE, normalize_suspect_terms  # noqa: E402

_TEST_TITLE_PREFIXES = ("E2E workflow", "E2E ", "Banc E")


def _load(path: pathlib.Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _present(text: str, phrase: str) -> bool:
    return bool(phrase) and re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text, flags=re.I) is not None


def _still_present(text: str, form: str, variants: list) -> int:
    """Variantes (≠ forme) encore présentes mot entier dans ``text``."""
    return sum(1 for v in variants
               if str(v).strip() and str(v).strip().lower() != form.lower() and _present(text, str(v).strip()))


def _real_job_ids(database_url: str | None) -> set[str] | None:
    if not database_url:
        return None
    import sqlalchemy as sa  # tiers ; importé ici pour laisser le script utilisable sans base

    engine = sa.create_engine(database_url)
    with engine.connect() as conn:
        rows = conn.execute(sa.text("select id, title, state from jobs")).fetchall()
    return {r[0] for r in rows if r[2] == "completed" and not str(r[1] or "").startswith(_TEST_TITLE_PREFIXES)}


def survey(jobs_dir: pathlib.Path, ids: set[str] | None) -> dict[str, int]:
    c: collections.Counter = collections.Counter()
    for d in sorted(p for p in jobs_dir.iterdir() if p.is_dir()):
        if ids is not None and d.name not in ids:
            continue
        ctx = _load(d / "context" / "meeting_context.json") or {}
        if not ctx:
            continue
        c["jobs"] += 1
        # Champs de contexte : suggestion LLM gardée telle quelle ou retouchée.
        pairs = (("title", "title_suggere"), ("topic", "sujet_suggere"),
                 ("objective", "objectif_suggere"), ("notes", "notes_suggeres"))
        for final, sugg in pairs:
            f, s = str(ctx.get(final) or "").strip(), str(ctx.get(sugg) or "").strip()
            if s:
                c[f"{final} : gardé"] += 1 if f == s else 0
                c[f"{final} : retouché"] += 1 if f != s else 0
        parts = _load(d / "context" / "participants.json") or []
        llm = ctx.get("speaker_roles_llm") or {}
        labels = {str((v or {}).get("label") if isinstance(v, dict) else v or "").strip().lower() for v in llm.values()}
        for p in parts:
            if not isinstance(p, dict):
                continue
            name = str(p.get("name") or "").strip().lower()
            if name:
                c["nom participant = étiquette LLM"] += 1 if name in labels else 0
                c["nom participant saisi/renommé"] += 1 if name not in labels else 0
            llm_roles = {str((v or {}).get("role") or "").strip() for v in llm.values() if isinstance(v, dict)}
            if p.get("role") and str(p.get("role")).strip() not in llm_roles:
                c["rôle participant retouché"] += 1
        # Termes douteux : proposés → validés → appliqués.
        prop = [e for e in ctx.get("termes_suspects") or [] if isinstance(e, dict)]
        val = [e for e in (_load(d / "context" / "session_lexicon.json") or []) if isinstance(e, dict)]
        c["termes proposés"] += len(prop)
        c["termes validés"] += len(val)
        c["termes proposés « A / B »"] += sum(1 for e in prop if ALTERNATIVES_RE.search(str(e.get("term") or "")))
        c["termes validés « A / B »"] += sum(1 for e in val if ALTERNATIVES_RE.search(str(e.get("term") or "")))
        pkeys = {str(e.get("term") or "").strip().lower() for e in prop}
        vkeys = {str(e.get("term") or "").strip().lower() for e in val}
        c["propositions rejetées"] += len(pkeys - vkeys)
        c["termes ajoutés par l'humain"] += len(vkeys - pkeys)
        c["replace_by renseigné"] += sum(1 for e in val if str(e.get("replace_by") or "").strip())
        cor = d / "metadata" / "transcription_corrigee.srt"
        cor_text = cor.read_text(encoding="utf-8", errors="ignore") if cor.exists() else ""
        if cor_text:
            for e in val:
                form = str(e.get("replace_by") or e.get("term") or "").strip()
                c["variantes validées encore dans le SRT corrigé"] += _still_present(cor_text, form, e.get("variants") or [])
            quick = d / "summary" / "quick_transcript.txt"
            quick_text = quick.read_text(encoding="utf-8", errors="ignore") if quick.exists() else ""
            after = normalize_suspect_terms(prop, quick_text)
            c["après normalisation : entrées"] += len(after)
            c["après normalisation : mentions séparées"] += sum(len(e.get("mentions") or []) for e in after)
            for e in after:
                form = str(e.get("replace_by") or e.get("term") or "").strip()
                c["après normalisation : variantes encore dans le SRT corrigé"] += _still_present(cor_text, form, e.get("variants") or [])
    return dict(c)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--jobs-dir", type=pathlib.Path, default=ROOT / "jobs")
    parser.add_argument("--database-url", default=None, help="Pour ne garder que les jobs terminés hors tests (titres E2E/banc exclus)")
    args = parser.parse_args()
    ids = _real_job_ids(args.database_url)
    for key, value in survey(args.jobs_dir, ids).items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

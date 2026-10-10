#!/usr/bin/env python3
"""Rapport de lecture du banc d'attribution (compagnon de attribution_bench.py).

Pour chaque run (JSON produit par le banc), imprime par SPEAKER_XX : ce que la LLM lui a
attribué (nom / fonction / rôle), son temps de parole, et l'ÉVIDENCE pour juger :

- ``--reference`` (fenêtre de référence préparée par prepare_reference_windows.py) : le
  SPEAKER_XX est rapproché de l'INTERVENANT de référence par recouvrement temporel des tours
  du job, et ses phrases de référence les plus longues sont citées ;
- sinon, les phrases du transcript rapide attribuées à ce locuteur (étiquetage acoustique).

Le verdict (attribution juste / fausse) reste une LECTURE humaine — le rapport la rend
possible en une page par run, sans ouvrir les jobs. Rien ne quitte la machine.

Usage :
    venv/bin/python scripts/bench/attribution_report.py bench_results/attribution/M2_*.json \\
        --reference bench_results/attribution/M2_T001_10-45/W01_001000_004500/reference.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _overlap_map(turns: list[dict], reference: list[dict]) -> dict[str, tuple[str, float]]:
    """SPEAKER_XX → (INTERVENANT de référence majoritaire, part du recouvrement)."""
    acc: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for t in turns:
        for r in reference:
            ov = min(float(t["end"]), float(r["end"])) - max(float(t["start"]), float(r["start"]))
            if ov > 0:
                acc[t["speaker"]][r["speaker"]] += ov
    out = {}
    for spk, per_ref in acc.items():
        total = sum(per_ref.values())
        best = max(per_ref.items(), key=lambda kv: kv[1])
        out[spk] = (best[0], best[1] / total if total else 0.0)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+", type=Path)
    ap.add_argument("--reference", type=Path, default=None, help="reference.json de la fenêtre (temps relatifs)")
    ap.add_argument("--jobs-dir", type=Path, default=_REPO / "jobs")
    args = ap.parse_args()

    reference = None
    if args.reference:
        reference = json.loads(args.reference.read_text(encoding="utf-8")).get("segments") or []
        ref_text: dict[str, list[str]] = defaultdict(list)
        for seg in reference:
            ref_text[seg["speaker"]].append(seg.get("text", "").strip())

    for run_path in args.runs:
        run = json.loads(run_path.read_text(encoding="utf-8"))
        print(f"\n# {run['label']} — mode {run['mode']} — diarisation {run.get('diarization_backend')}")
        print(f"résumé {run.get('t_summary_s')} s · {run.get('speaker_count_diar')} locuteurs détectés · "
              f"{run.get('speaker_count_llm')} décrits par la LLM · couverture étiquetée "
              f"{run.get('labeled_word_ratio')} · lignes « certaines » du contexte : {run.get('context_certain_lines')}")
        if run.get("summary_error"):
            print(f"ERREUR résumé : {run['summary_error']}")
        roles = run.get("speaker_roles_llm") or {}
        times = run.get("speaking_time_s") or {}
        evidence = run.get("evidence") or {}
        ref_map = {}
        if reference is not None and run.get("job_id"):
            turns_path = args.jobs_dir / run["job_id"] / "speakers" / "speaker_turns.json"
            if turns_path.exists():
                turns = (json.loads(turns_path.read_text(encoding="utf-8")).get("exclusive_turns")
                         or json.loads(turns_path.read_text(encoding="utf-8")).get("turns") or [])
                ref_map = _overlap_map(turns, reference)
        for spk in sorted(set(roles) | set(times), key=lambda s: -times.get(s, 0)):
            info = roles.get(spk) or {}
            print(f"\n## {spk} — {times.get(spk, 0):.0f} s de parole")
            print(f"LLM : nom « {info.get('label', '')} » · fonction « {info.get('function', '')} » · "
                  f"rôle « {info.get('role', '')} »" if info else "LLM : (non décrit)")
            if ref_map.get(spk):
                ref_id, share = ref_map[spk]
                print(f"Référence : {ref_id} ({share:.0%} du recouvrement)")
                for line in sorted(ref_text.get(ref_id, []), key=len, reverse=True)[:4]:
                    print(f"  · {line[:220]}")
            for line in (evidence.get(spk) or [])[:4]:
                print(f"  » {line[:220]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Consensus inter-moteurs STT : parole sûre vs zones à hallucination.

Protocole (né du chantier anti-hallucination, 2026-08-04, cf.
archives/audio_tests/test5_inaudible_distille.md) : transcrire LE MÊME audio avec
plusieurs moteurs (`tests/test_e2e_workflow.py --stt-backend X --skip-llm --keep
--output-json runs/X.json`), puis comparer par fenêtres temporelles :

- une fenêtre où plusieurs moteurs produisent un texte lexicalement PROCHE
  (Jaccard sur tokens normalisés) = parole réelle — même chuchotée, même si un
  moteur isolé la classait « hallucination probable » (vécu : test5) ;
- une fenêtre où ils produisent des textes DIVERGENTS (chacun invente autre
  chose) = bruit hallucinogène — matière première du catalogue par moteur
  (transcria/data/hallucination_signatures.yaml) et des corpus adversariaux.

Garde-fou (vécu : granite replié silencieusement sur cohere, doublon parfait ;
puis 3 backends servis non configurés = clones de cohere, minage 2026-08-05) :
deux moteurs au transcript identique = un seul moteur réel — le doublon est
détecté et EXCLU, avec avertissement. Vérifier aussi `effective_stt_backend`
dans les JSON du gate (contrôle `backend_effectif` depuis 2026-08-04).
Limite connue : sur un texte LU court et propre (régime dictée), de bons moteurs
convergent honnêtement vers un transcript identique — sur ce régime, l'exclusion
est un faux positif à arbitrer à l'œil (vécu 2026-08-05 : whisper == parakeet).

Usage :
    venv/bin/python scripts/bench/stt_consensus.py runs/*.json
    venv/bin/python scripts/bench/stt_consensus.py runs/*.json --zones-json zones.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from itertools import combinations
from pathlib import Path

BIN_S = 0.5
MIN_ENGINES_SPEECH = 4
AGREE_SPEECH = 0.25
MAX_AGREE_NOISE = 0.12
MIN_ENGINES_NOISE = 2


def normalize_tokens(text: str) -> set[str]:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return set(re.findall(r"[a-z0-9]{2,}", text))


def load_run(path: Path) -> tuple[str, list[dict]] | None:
    """(nom du moteur EFFECTIF, segments) depuis un JSON du gate E2E, ou None."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        job_dir = data.get("job_dir")
        segments = json.loads(
            (Path(job_dir) / "metadata" / "transcription_segments.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        print(f"[ignoré] {path} : illisible ou sans job_dir/segments", file=sys.stderr)
        return None
    requested = str(data.get("stt_backend") or path.stem)
    effective = str(data.get("effective_stt_backend") or requested)
    if effective != requested:
        print(f"[ATTENTION] {path.stem} : backend effectif « {effective} » ≠ demandé "
              f"« {requested} » (repli du pipeline) — étiqueté {effective}", file=sys.stderr)
    return effective, segments


def full_text(segments: list[dict]) -> str:
    return " ".join(str(s.get("text") or "").strip() for s in segments).strip()


def drop_duplicates(engines: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Deux moteurs au transcript identique = un seul moteur réel (vécu granite/cohere)."""
    kept: dict[str, list[dict]] = {}
    seen: dict[str, str] = {}
    for name in sorted(engines):
        text = full_text(engines[name])
        if text and text in seen:
            print(f"[ATTENTION] {name} : transcript IDENTIQUE à {seen[text]} — doublon exclu "
                  "(repli silencieux probable)", file=sys.stderr)
            continue
        if text:
            seen[text] = name
        kept[name] = engines[name]
    return kept


def tokens_in_window(segments: list[dict], t0: float, t1: float) -> set[str]:
    tokens: set[str] = set()
    for seg in segments:
        s, e = float(seg.get("start") or 0), float(seg.get("end") or 0)
        if min(e, t1) - max(s, t0) > 0:
            tokens |= normalize_tokens(str(seg.get("text") or ""))
    return tokens


def jaccard(a: set[str], b: set[str]) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 1.0


def classify_bins(engines: dict[str, list[dict]], duration: float) -> list[dict]:
    rows = []
    for i in range(int(duration / BIN_S) + 1):
        t0, t1 = i * BIN_S, (i + 1) * BIN_S
        producers = {n: t for n, segs in engines.items()
                     if (t := tokens_in_window(segs, t0, t1))}
        if len(producers) >= 2:
            pairs = list(combinations(producers.values(), 2))
            agreement = sum(jaccard(a, b) for a, b in pairs) / len(pairs)
        else:
            agreement = 0.0
        if len(producers) >= MIN_ENGINES_SPEECH and agreement >= AGREE_SPEECH:
            label = "PAROLE"
        elif len(producers) >= MIN_ENGINES_NOISE and agreement <= MAX_AGREE_NOISE:
            label = "BRUIT!"
        elif producers:
            label = "douteux"
        else:
            label = "silence"
        rows.append({"t0": round(t0, 2), "t1": round(t1, 2), "n": len(producers),
                     "agree": round(agreement, 2), "label": label})
    return rows


def merge_zones(rows: list[dict]) -> list[dict]:
    zones: list[dict] = []
    for row in rows:
        if zones and zones[-1]["label"] == row["label"]:
            zones[-1]["t1"] = row["t1"]
            zones[-1]["agree"] = round((zones[-1]["agree"] + row["agree"]) / 2, 2)
            zones[-1]["n"] = max(zones[-1]["n"], row["n"])
        else:
            zones.append(dict(row))
    return zones


def texts_in_zone(segments: list[dict], t0: float, t1: float) -> str:
    """Textes des segments MAJORITAIREMENT dans la zone (recouvrement ≥ 50 % de la
    durée du segment) — un segment de 30 s qui effleure une zone de 2 s n'apprend
    rien sur elle (vécu au minage 2026-08-05 : les moteurs mono-segment inondaient
    l'inventaire des zones BRUIT! de vraie parole)."""
    parts = []
    for s in segments:
        ss, se = float(s.get("start") or 0), float(s.get("end") or 0)
        overlap = min(se, t1) - max(ss, t0)
        if overlap > 0 and overlap / max(se - ss, 0.01) >= 0.5:
            txt = str(s.get("text") or "").strip()
            if txt:
                parts.append(txt)
    return " | ".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runs", nargs="+", type=Path,
                        help="JSON de runs du gate E2E (--output-json), un par moteur")
    parser.add_argument("--zones-json", type=Path, default=None,
                        help="écrire les zones fusionnées dans ce fichier")
    args = parser.parse_args(argv)

    engines: dict[str, list[dict]] = {}
    for path in args.runs:
        loaded = load_run(path)
        if loaded is not None:
            engines[loaded[0]] = loaded[1]
    engines = drop_duplicates(engines)
    print(f"Moteurs réellement distincts : {len(engines)} → {sorted(engines)}")
    if len(engines) < 3:
        print("Trop peu de moteurs distincts (< 3) — consensus non significatif.")
        return 1

    duration = max((float(s.get("end") or 0) for segs in engines.values() for s in segs),
                   default=0.0)
    zones = merge_zones(classify_bins(engines, duration))

    print(f"\n{'zone':>16}  {'moteurs':>7}  {'accord':>6}  label")
    for z in zones:
        print(f"{z['t0']:7.1f}-{z['t1']:5.1f}s  {z['n']:>7}  {z['agree']:>6}  {z['label']}")

    noisy = [z for z in zones if z["label"] == "BRUIT!"]
    if noisy:
        print("\n--- Ce que les moteurs inventent sur les zones BRUIT! ---")
        for z in noisy:
            print(f"\n[{z['t0']:.1f}-{z['t1']:.1f}s]")
            for name, segs in sorted(engines.items()):
                sample = texts_in_zone(segs, z["t0"], z["t1"])
                if sample:
                    print(f"  {name:<12} {sample[:90]}")
        cuts = "+".join(f"[{z['t0']}-{z['t1']}]" for z in noisy)
        print(f"\nDistillat adversarial possible (ffmpeg atrim) : {cuts}")

    if args.zones_json:
        args.zones_json.write_text(json.dumps(zones, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
        print(f"Zones écrites : {args.zones_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

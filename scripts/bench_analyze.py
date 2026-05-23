#!/usr/bin/env python3
"""
TranscrIA — analyse locale des résultats de bench (sans LLM).

Lit tous les JSON d'un répertoire de bench et produit :
  - Tableau comparatif Cohere vs Whisper (segments, mots, timing, VRAM)
  - Détection d'hallucinations (no_speech_prob élevé, mots peu confiants)
  - Classement des combos par qualité estimée
  - Export Markdown + CSV

Utilisation :
    python scripts/bench_analyze.py --bench-dir bench_results/test7_all
    python scripts/bench_analyze.py --bench-dir bench_results/test2_all --bench-dir bench_results/test7_all
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(
    format="%(asctime)s [bench_analyze] %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
    stream=sys.stderr,
)
logger = logging.getLogger("bench_analyze")

NSP_THRESHOLD = 0.5
LOW_CONF_MIN = 0.4
LOW_CONF_RATIO = 0.5


# ─────────────────────────────────────────────────────────────────────────────
# Chargement
# ─────────────────────────────────────────────────────────────────────────────

def load_bench_dir(bench_dir: Path) -> list[dict]:
    results = []
    patterns = ["[0-9][0-9][0-9].json", "E[0-9][0-9].json"]
    seen: set[Path] = set()
    for pattern in patterns:
        for p in sorted(bench_dir.glob(pattern)):
            if p in seen:
                continue
            seen.add(p)
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                d["_source_dir"] = str(bench_dir)
                results.append(d)
            except Exception as exc:
                logger.warning("JSON illisible : %s (%s)", p.name, exc)
    results.sort(key=lambda r: (r.get("_source_dir", ""), r.get("combo_id") or ""))
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Analyse par combo
# ─────────────────────────────────────────────────────────────────────────────

def analyze_combo(d: dict) -> dict:
    combo_id = d.get("combo_id", "?")
    stt = d.get("stt_backend", "?")
    status = d.get("status", "?")
    skip_dia = bool(d.get("skip_diarization"))
    overrides = d.get("config_overrides") or []
    source_dir = Path(d.get("_source_dir", ".")).name

    timings = d.get("timings") or {}
    t_summ = (timings.get("summary_stt_s")
              or timings.get("summary_s")
              or timings.get("stt_s")
              or 0.0)
    t_pipe = timings.get("pipeline_s") or 0.0
    t_total = d.get("_elapsed_wall_s") or (t_summ + t_pipe)
    vram = d.get("vram_peak_mb") or 0

    segs = d.get("transcription_segments") or []
    n_segs = len(segs)
    all_words: list[dict] = []
    for s in segs:
        all_words.extend(s.get("words") or [])
    n_words = len(all_words)

    # Hallucinations — no_speech_prob
    nsp_values = [s["no_speech_prob"] for s in segs if s.get("no_speech_prob") is not None]
    nsp_high = [v for v in nsp_values if v > NSP_THRESHOLD]
    nsp_max = max(nsp_values) if nsp_values else None
    nsp_mean = sum(nsp_values) / len(nsp_values) if nsp_values else None

    # Hallucinations — confiance mots
    suspect_segs = 0
    total_low_words = 0
    for s in segs:
        words = s.get("words") or []
        if not words:
            continue
        low = sum(1 for w in words if w.get("probability", 1.0) < LOW_CONF_MIN)
        total_low_words += low
        if low / len(words) > LOW_CONF_RATIO:
            suspect_segs += 1

    low_word_ratio = total_low_words / n_words if n_words else None

    # Score qualité estimé (heuristique, plus c'est bas mieux c'est)
    # nsp_high seul sans mots peu confiants = faux positif probable (vrai texte sur musique)
    # on compte uniquement les segments où nsp_high ET mots peu confiants ou texte suspect
    nsp_with_low_words = 0
    for s in segs:
        nsp = s.get("no_speech_prob")
        if nsp is None or nsp <= NSP_THRESHOLD:
            continue
        words = s.get("words") or []
        low = sum(1 for w in words if w.get("probability", 1.0) < LOW_CONF_MIN)
        ratio = low / len(words) if words else 0.0
        # Faux positif probable : mots tous confiants → probable vrai contenu sur bruit
        if ratio > 0.1 or not words:
            nsp_with_low_words += 1

    hallucination_score = nsp_with_low_words + suspect_segs * 2

    return {
        "source": source_dir,
        "combo_id": combo_id,
        "stt": stt,
        "status": status,
        "skip_dia": skip_dia,
        "overrides": "|".join(overrides) if overrides else "",
        "t_stt_s": round(t_summ, 1),
        "t_total_s": round(t_total, 1),
        "vram_mb": vram,
        "n_segs": n_segs,
        "n_words": n_words,
        "nsp_high": len(nsp_high),
        "nsp_max": round(nsp_max, 3) if nsp_max is not None else None,
        "nsp_mean": round(nsp_mean, 3) if nsp_mean is not None else None,
        "suspect_segs": suspect_segs,
        "low_word_ratio": round(low_word_ratio, 3) if low_word_ratio is not None else None,
        "hallucination_score": hallucination_score,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Rapport Markdown
# ─────────────────────────────────────────────────────────────────────────────

def _fmt(v, fmt=".1f", na="—"):
    if v is None:
        return na
    return format(v, fmt)


def write_report(rows: list[dict], output: Path) -> None:
    sources = sorted({r["source"] for r in rows})

    lines = [
        "# Analyse bench TranscrIA — comparatif Cohere vs Whisper",
        "",
        f"Fichiers analysés : {', '.join(sources)}",
        f"Total combos : {len(rows)}",
        "",
    ]

    # ── Tableau principal ────────────────────────────────────────────────────
    lines += [
        "## Tableau comparatif",
        "",
        "| Source | ID | STT | dia | status | t_stt | t_tot | VRAM | segs | mots | nsp_high | nsp_max | sus_segs | low_word% | hall_score | overrides |",
        "|--------|----|----|-----|--------|-------|-------|------|------|------|----------|---------|----------|-----------|------------|-----------|",
    ]
    for r in rows:
        dia = "✗" if r["skip_dia"] else "✓"
        nsp_h = r["nsp_high"] if r["nsp_high"] else "—"
        sus = r["suspect_segs"] if r["suspect_segs"] else "—"
        lwr = f"{r['low_word_ratio']:.1%}" if r["low_word_ratio"] is not None else "—"
        hall = f"**{r['hallucination_score']}**" if r["hallucination_score"] > 0 else "0"
        lines.append(
            f"| {r['source']} | {r['combo_id']} | {r['stt']:7s} | {dia} | {r['status']:6s}"
            f" | {_fmt(r['t_stt_s'])}s | {_fmt(r['t_total_s'])}s | {r['vram_mb']:4d}M"
            f" | {r['n_segs']:4d} | {r['n_words']:5d}"
            f" | {nsp_h} | {_fmt(r['nsp_max'], '.2f')} | {sus} | {lwr} | {hall}"
            f" | {r['overrides'][:40] or '—'} |"
        )
    lines.append("")

    # ── Résumé Cohere vs Whisper ─────────────────────────────────────────────
    lines += ["## Résumé Cohere vs Whisper", ""]
    for stt in ("cohere", "whisper"):
        subset = [r for r in rows if r["stt"] == stt and r["status"] == "ok"]
        if not subset:
            continue
        avg_stt = sum(r["t_stt_s"] for r in subset) / len(subset)
        avg_total = sum(r["t_total_s"] for r in subset) / len(subset)
        avg_segs = sum(r["n_segs"] for r in subset) / len(subset)
        avg_words = sum(r["n_words"] for r in subset) / len(subset)
        total_hall = sum(r["hallucination_score"] for r in subset)
        avg_nsp = [r["nsp_mean"] for r in subset if r["nsp_mean"] is not None]
        avg_nsp_s = f"{sum(avg_nsp)/len(avg_nsp):.3f}" if avg_nsp else "—"
        lines += [
            f"**{stt.capitalize()}** ({len(subset)} combos OK) :",
            f"- Durée STT moyenne : {avg_stt:.1f}s",
            f"- Durée totale moyenne : {avg_total:.1f}s",
            f"- Segments moyens : {avg_segs:.1f}",
            f"- Mots moyens : {avg_words:.1f}",
            f"- no_speech_prob moyen : {avg_nsp_s}",
            f"- Score hallucination total : {total_hall}",
            "",
        ]

    # ── Top hallucinations ───────────────────────────────────────────────────
    problematic = sorted(
        [r for r in rows if r["hallucination_score"] > 0],
        key=lambda r: -r["hallucination_score"],
    )
    if problematic:
        lines += ["## Combos avec hallucinations détectées", ""]
        for r in problematic[:10]:
            lines.append(
                f"- **{r['combo_id']}** ({r['stt']}, {'no-dia' if r['skip_dia'] else 'dia'}) "
                f"— score={r['hallucination_score']}, "
                f"nsp_high={r['nsp_high']}/{r['n_segs']}, "
                f"nsp_max={_fmt(r['nsp_max'], '.2f')}, "
                f"suspect_segs={r['suspect_segs']}"
                + (f", overrides: {r['overrides'][:60]}" if r["overrides"] else "")
            )
        lines.append("")

    # ── Segments hallucinés détaillés ────────────────────────────────────────
    lines += ["## Détail des segments suspects (nsp > 0.5 ou mots peu confiants)", ""]
    segment_count = 0
    for d_source in _raw_data:
        combo_id = d_source.get("combo_id", "?")
        stt = d_source.get("stt_backend", "?")
        segs = d_source.get("transcription_segments") or []
        for s in segs:
            nsp = s.get("no_speech_prob")
            words = s.get("words") or []
            low = sum(1 for w in words if w.get("probability", 1.0) < LOW_CONF_MIN)
            low_ratio = low / len(words) if words else 0.0
            is_suspect = (nsp is not None and nsp > NSP_THRESHOLD) or (words and low_ratio > LOW_CONF_RATIO)
            if not is_suspect:
                continue
            nsp_s = f"{nsp:.2f}" if nsp is not None else "N/A"
            lw_s = f"{low}/{len(words)} ({low_ratio:.0%})" if words else "N/A"
            text = s.get("text", "").strip()[:100]
            lines.append(
                f"- [{combo_id}/{stt}] `[{s['start']:.1f}-{s['end']:.1f}s]` "
                f"nsp={nsp_s} low_words={lw_s} | *{text}*"
            )
            segment_count += 1
    if segment_count == 0:
        lines.append("*Aucun segment suspect détecté.*")
    lines.append("")

    lines += [
        "---",
        f"*Généré par bench_analyze.py — seuils : nsp>{NSP_THRESHOLD}, low_conf<{LOW_CONF_MIN}, ratio>{LOW_CONF_RATIO}*",
    ]

    output.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Rapport écrit : %s", output)


# ─────────────────────────────────────────────────────────────────────────────
# CSV
# ─────────────────────────────────────────────────────────────────────────────

def write_csv(rows: list[dict], output: Path) -> None:
    if not rows:
        return
    fieldnames = list(rows[0].keys())
    with output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    logger.info("CSV écrit : %s", output)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

_raw_data: list[dict] = []


def main() -> None:
    global _raw_data

    parser = argparse.ArgumentParser(
        description="Analyse locale des résultats de bench (sans LLM)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--bench-dir", type=Path, action="append", dest="bench_dirs", required=True,
        metavar="DIR",
        help="Répertoire(s) de bench (peut être répété pour comparer plusieurs runs)",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="Fichier Markdown de sortie (défaut: <premier-bench-dir>/analysis.md)",
    )
    parser.add_argument(
        "--csv", type=Path, default=None,
        help="Fichier CSV de sortie (défaut: <premier-bench-dir>/analysis.csv)",
    )
    args = parser.parse_args()

    all_results: list[dict] = []
    for bench_dir in args.bench_dirs:
        if not bench_dir.is_dir():
            logger.error("Répertoire introuvable : %s", bench_dir)
            sys.exit(1)
        loaded = load_bench_dir(bench_dir)
        if not loaded:
            logger.warning("Aucun JSON trouvé dans %s", bench_dir)
        else:
            logger.info("%d combo(s) chargé(s) depuis %s", len(loaded), bench_dir)
        _raw_data.extend(loaded)
        all_results.extend(loaded)

    if not all_results:
        logger.error("Aucun résultat à analyser")
        sys.exit(1)

    rows = [analyze_combo(d) for d in all_results]

    output_md = args.output or (args.bench_dirs[0] / "analysis.md")
    output_csv = args.csv or (args.bench_dirs[0] / "analysis.csv")

    write_report(rows, output_md)
    write_csv(rows, output_csv)

    # Résumé console
    ok = sum(1 for r in rows if r["status"] == "ok")
    hall = sum(1 for r in rows if r["hallucination_score"] > 0)
    cohere_ok = [r for r in rows if r["stt"] == "cohere" and r["status"] == "ok"]
    whisper_ok = [r for r in rows if r["stt"] == "whisper" and r["status"] == "ok"]

    print(f"\n{'='*60}")
    print(f"  {ok}/{len(rows)} combos OK — {hall} avec hallucinations")
    if cohere_ok:
        avg = sum(r["t_total_s"] for r in cohere_ok) / len(cohere_ok)
        hall_c = sum(r["hallucination_score"] for r in cohere_ok)
        print(f"  Cohere  : {len(cohere_ok)} combos, t_moy={avg:.1f}s, hall_total={hall_c}")
    if whisper_ok:
        avg = sum(r["t_total_s"] for r in whisper_ok) / len(whisper_ok)
        hall_w = sum(r["hallucination_score"] for r in whisper_ok)
        print(f"  Whisper : {len(whisper_ok)} combos, t_moy={avg:.1f}s, hall_total={hall_w}")
    print(f"{'='*60}")
    print(f"  Rapport : {output_md}")
    print(f"  CSV     : {output_csv}")
    print()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
TranscrIA — analyse locale des résultats de bench (sans LLM).

Lit tous les JSON d'un répertoire de bench et produit :
  - Tableau comparatif par backend (segments, mots, timing, VRAM)
  - Colonnes de calibration STT (audio_corpus, SQUIM/DNSMOS, reliability)
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
import re
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
MIN_SCHEMA_VERSION = 2
MIN_AUDIO_CORPUS_SCHEMA_VERSION = 1
SRT_SNIPPET_CHARS = 140
MAX_QUALITATIVE_SNIPPETS = 5

NON_LATIN_SCRIPT_PATTERNS = {
    "arabic": re.compile(r"[\u0600-\u06ff]"),
    "cjk": re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]"),
    "cyrillic": re.compile(r"[\u0400-\u04ff]"),
}
GENERIC_HALLUCINATION_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bthank you(?: very much)?\b",
        r"\bthanks for watching\b",
        r"\bmerci d['’]avoir regard[ée] cette vid[ée]o\b",
        r"\bn['’]oubliez pas de vous abonner\b",
        r"\babonnez-vous\b",
        r"\blike and subscribe\b",
    )
]


# ─────────────────────────────────────────────────────────────────────────────
# Chargement
# ─────────────────────────────────────────────────────────────────────────────

def load_bench_dir(bench_dir: Path) -> list[dict]:
    results = []
    patterns = ["[0-9][0-9][0-9].json", "E[0-9][0-9].json", "S[0-9][0-9].json", "V[0-9][0-9].json"]
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


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _round(value, digits: int = 3):
    value = _as_float(value)
    return round(value, digits) if value is not None else None


def _join_overrides(overrides) -> str:
    if isinstance(overrides, dict):
        return "|".join(f"{k}={v}" for k, v in sorted(overrides.items()))
    if isinstance(overrides, list):
        return "|".join(str(item) for item in overrides)
    return ""


def _reliability_count(counts: dict, level: str) -> int:
    if not isinstance(counts, dict):
        return 0
    return _as_int(counts.get(level), 0)


def _srt_raw_content(d: dict) -> str:
    srt = d.get("srt") or {}
    if isinstance(srt, dict):
        raw = srt.get("raw_content")
        if isinstance(raw, str):
            return raw
    return ""


def _compact_text(text: str, limit: int = SRT_SNIPPET_CHARS) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _extract_srt_text_blocks(raw_content: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    for line in raw_content.splitlines():
        stripped = line.strip()
        if not stripped:
            if current:
                blocks.append(" ".join(current))
                current = []
            continue
        if stripped.isdigit() or "-->" in stripped:
            continue
        current.append(stripped)
    if current:
        blocks.append(" ".join(current))
    return blocks


def _count_non_latin_scripts(text: str) -> dict[str, int]:
    return {name: len(pattern.findall(text)) for name, pattern in NON_LATIN_SCRIPT_PATTERNS.items()}


def qualitative_review(d: dict) -> dict:
    """Retourne des signaux de relecture, pas un verdict qualité automatique."""
    raw_content = _srt_raw_content(d)
    blocks = _extract_srt_text_blocks(raw_content)
    full_text = "\n".join(blocks)
    reasons: list[str] = []
    snippets: list[str] = []

    script_counts = _count_non_latin_scripts(full_text)
    present_scripts = [name for name, count in script_counts.items() if count > 0]
    if present_scripts:
        reasons.append("script_non_latin")
        for block in blocks:
            if any(NON_LATIN_SCRIPT_PATTERNS[name].search(block) for name in present_scripts):
                snippets.append(_compact_text(block))
                if len(snippets) >= MAX_QUALITATIVE_SNIPPETS:
                    break

    generic_hits: list[str] = []
    for block in blocks:
        if any(pattern.search(block) for pattern in GENERIC_HALLUCINATION_PATTERNS):
            generic_hits.append(_compact_text(block))
    if generic_hits:
        reasons.append("phrase_generique_suspecte")
        snippets.extend(generic_hits[:MAX_QUALITATIVE_SNIPPETS])

    srt = d.get("srt") or {}
    raw_words = _as_int(srt.get("raw_words")) if isinstance(srt, dict) else 0
    raw_segments = _as_int(srt.get("raw_segments")) if isinstance(srt, dict) else 0
    if raw_segments > 0 and raw_words <= 10:
        reasons.append("transcription_tres_courte")
    if raw_segments > 0 and raw_words > 0 and raw_words / raw_segments <= 2.0:
        reasons.append("micro_fragments_possibles")

    segments = d.get("transcription_segments") or []
    very_short_segments = 0
    for segment in segments:
        duration = _as_float(segment.get("end"), 0.0) - _as_float(segment.get("start"), 0.0)
        if duration is not None and duration > 0 and duration < 0.5:
            very_short_segments += 1
    if very_short_segments >= 3:
        reasons.append("segments_tres_courts")

    deduped_snippets = []
    seen = set()
    for snippet in snippets:
        if snippet in seen:
            continue
        seen.add(snippet)
        deduped_snippets.append(snippet)
        if len(deduped_snippets) >= MAX_QUALITATIVE_SNIPPETS:
            break

    return {
        "review_required": bool(reasons),
        "review_reasons": "|".join(reasons),
        "non_latin_scripts": "|".join(present_scripts),
        "generic_hallucination_hits": len(generic_hits),
        "very_short_segments": very_short_segments,
        "review_snippets": deduped_snippets,
        "manual_verdict": "",
        "manual_notes": "",
    }


def _result_schema_errors(d: dict) -> list[str]:
    errors: list[str] = []
    if _as_int(d.get("schema_version"), 0) < MIN_SCHEMA_VERSION:
        errors.append("schema_version<2")
    audio_corpus = d.get("audio_corpus")
    if not isinstance(audio_corpus, dict):
        errors.append("audio_corpus_absent")
    elif _as_int(audio_corpus.get("schema_version"), 0) < MIN_AUDIO_CORPUS_SCHEMA_VERSION:
        errors.append("audio_corpus.schema_version<1")
    if not isinstance(d.get("transcription_metadata"), dict):
        errors.append("transcription_metadata_absent")
    if not isinstance(d.get("segment_reliability_counts"), dict):
        errors.append("segment_reliability_counts_absent")
    return errors


def split_supported_results(results: list[dict]) -> tuple[list[dict], list[dict]]:
    supported: list[dict] = []
    ignored: list[dict] = []
    for result in results:
        errors = _result_schema_errors(result)
        if errors:
            item = dict(result)
            item["_schema_errors"] = errors
            ignored.append(item)
        else:
            supported.append(result)
    return supported, ignored


# ─────────────────────────────────────────────────────────────────────────────
# Analyse par combo
# ─────────────────────────────────────────────────────────────────────────────

def analyze_combo(d: dict) -> dict:
    combo_id = d.get("combo_id", "?")
    stt = d.get("stt_backend", "?")
    effective_stt = d.get("effective_stt_backend") or stt
    status = d.get("status", "?")
    skip_dia = bool(d.get("skip_diarization"))
    overrides = d.get("config_overrides") or {}
    source_dir = Path(d.get("_source_dir", ".")).name

    timings = d.get("timings") or {}
    t_summ = (timings.get("summary_stt_s")
              or timings.get("summary_s")
              or timings.get("stt_s")
              or 0.0)
    t_pipe = timings.get("pipeline_s") or 0.0
    t_total = d.get("_elapsed_wall_s") or (t_summ + t_pipe)
    vram = d.get("vram_peak_mb") or 0

    srt = d.get("srt") or {}
    audio_corpus = d.get("audio_corpus") or {}
    difficulty = audio_corpus.get("difficulty_summary") or {}
    squim = audio_corpus.get("squim_global") or {}
    dnsmos = audio_corpus.get("dnsmos_global") or {}
    transcription_metadata = d.get("transcription_metadata") or {}
    quality_decision = d.get("quality_decision") or {}
    reliability_counts = d.get("segment_reliability_counts") or {}

    segs = d.get("transcription_segments") or []
    n_segs = len(segs) or _as_int(srt.get("raw_segments")) or _as_int(transcription_metadata.get("segments"))
    all_words: list[dict] = []
    for s in segs:
        all_words.extend(s.get("words") or [])
    n_words = len(all_words) or _as_int(srt.get("raw_words"))

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
    review = qualitative_review(d)

    return {
        "source": source_dir,
        "schema_version": _as_int(d.get("schema_version")),
        "combo_id": combo_id,
        "stt": stt,
        "effective_stt": effective_stt,
        "status": status,
        "skip_dia": skip_dia,
        "mode": d.get("mode", ""),
        "gpu": d.get("gpu") or "",
        "overrides": _join_overrides(overrides),
        "t_stt_s": round(t_summ, 1),
        "t_total_s": round(t_total, 1),
        "pipeline_s": round(_as_float(t_pipe, 0.0) or 0.0, 1),
        "vram_mb": _as_int(vram),
        "risk_level": audio_corpus.get("risk_level") or "",
        "audio_flags": "|".join(str(flag) for flag in (audio_corpus.get("flags") or [])),
        "snr_db": _round(audio_corpus.get("snr_db"), 2),
        "bandwidth_95_hz": _round(audio_corpus.get("bandwidth_95_hz"), 1),
        "squim_stoi": _round(squim.get("stoi"), 3),
        "squim_pesq": _round(squim.get("pesq"), 3),
        "squim_sisdr": _round(squim.get("sisdr"), 3),
        "dnsmos_sig": _round(dnsmos.get("sig"), 3),
        "dnsmos_bak": _round(dnsmos.get("bak"), 3),
        "dnsmos_ovrl": _round(dnsmos.get("ovrl"), 3),
        "difficulty_windows": _as_int(difficulty.get("windows")),
        "difficulty_degrade_ratio": _round(difficulty.get("degrade_ratio"), 4),
        "difficulty_worst": difficulty.get("worst") or "",
        "chunking_mode": transcription_metadata.get("chunking_mode") or "",
        "chunk_metrics": json.dumps(transcription_metadata.get("chunk_metrics") or {}, ensure_ascii=False, sort_keys=True),
        "quality_level": quality_decision.get("level") or "",
        "n_segs": n_segs,
        "n_words": n_words,
        "rel_ok": _reliability_count(reliability_counts, "ok"),
        "rel_suspect": _reliability_count(reliability_counts, "suspect"),
        "rel_degrade": _reliability_count(reliability_counts, "degrade"),
        "nsp_high": len(nsp_high),
        "nsp_max": round(nsp_max, 3) if nsp_max is not None else None,
        "nsp_mean": round(nsp_mean, 3) if nsp_mean is not None else None,
        "suspect_segs": suspect_segs,
        "low_word_ratio": round(low_word_ratio, 3) if low_word_ratio is not None else None,
        "hallucination_score": hallucination_score,
        "review_required": review["review_required"],
        "review_reasons": review["review_reasons"],
        "non_latin_scripts": review["non_latin_scripts"],
        "generic_hallucination_hits": review["generic_hallucination_hits"],
        "very_short_segments": review["very_short_segments"],
        "review_snippets": " || ".join(review["review_snippets"]),
        "manual_verdict": review["manual_verdict"],
        "manual_notes": review["manual_notes"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Rapport Markdown
# ─────────────────────────────────────────────────────────────────────────────

def _fmt(v, fmt=".1f", na="—"):
    if v is None:
        return na
    return format(v, fmt)


def _md_cell(value) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def write_report(rows: list[dict], output: Path, ignored: list[dict] | None = None) -> None:
    ignored = ignored or []
    sources = sorted({r["source"] for r in rows})

    lines = [
        "# Analyse bench TranscrIA — calibration STT",
        "",
        f"Fichiers analysés : {', '.join(sources)}",
        f"Total combos : {len(rows)}",
        f"Runs ignorés (ancien format / contrat incomplet) : {len(ignored)}",
        "",
    ]
    if ignored:
        lines += [
            "> Les runs ignorés ne sont pas mélangés aux statistiques : ils n'ont pas",
            f"> `schema_version >= {MIN_SCHEMA_VERSION}` ou le bloc `audio_corpus` requis.",
            "",
        ]

    # ── Tableau principal ────────────────────────────────────────────────────
    lines += [
        "## Tableau comparatif",
        "",
        "| Source | ID | STT | eff | dia | status | risque | diff% | chunking | t_tot | VRAM | mots | "
        "rel D/S/O | nsp_high | hall_score | overrides |",
        "|--------|----|-----|-----|-----|--------|--------|-------|----------|-------|------|------|-----------|----------|------------|-----------|",
    ]
    for r in rows:
        dia = "✗" if r["skip_dia"] else "✓"
        nsp_h = r["nsp_high"] if r["nsp_high"] else "—"
        diff = f"{r['difficulty_degrade_ratio']:.1%}" if r["difficulty_degrade_ratio"] is not None else "—"
        rel = f"{r['rel_degrade']}/{r['rel_suspect']}/{r['rel_ok']}"
        hall = f"**{r['hallucination_score']}**" if r["hallucination_score"] > 0 else "0"
        lines.append(
            f"| {r['source']} | {r['combo_id']} | {r['stt']} | {r['effective_stt']} | {dia} | {r['status']}"
            f" | {r['risk_level'] or '—'} | {diff} | {r['chunking_mode'] or '—'}"
            f" | {_fmt(r['t_total_s'])}s | {r['vram_mb']:4d}M | {r['n_words']:5d}"
            f" | {rel} | {nsp_h} | {hall}"
            f" | {r['overrides'][:40] or '—'} |"
        )
    lines.append("")

    # ── Calibration audio ───────────────────────────────────────────────────
    lines += [
        "## Calibration audio",
        "",
        "| Risque | Runs | diff% moy | PESQ moy | DNSMOS OVRL moy | rel degrade | hall_score |",
        "|--------|------|-----------|----------|------------------|-------------|------------|",
    ]
    for risk in sorted({r["risk_level"] or "unknown" for r in rows}):
        subset = [r for r in rows if (r["risk_level"] or "unknown") == risk]
        diff_values = [r["difficulty_degrade_ratio"] for r in subset if r["difficulty_degrade_ratio"] is not None]
        pesq_values = [r["squim_pesq"] for r in subset if r["squim_pesq"] is not None]
        ovrl_values = [r["dnsmos_ovrl"] for r in subset if r["dnsmos_ovrl"] is not None]
        diff_avg = sum(diff_values) / len(diff_values) if diff_values else None
        pesq_avg = sum(pesq_values) / len(pesq_values) if pesq_values else None
        ovrl_avg = sum(ovrl_values) / len(ovrl_values) if ovrl_values else None
        lines.append(
            f"| {risk} | {len(subset)} | "
            f"{(f'{diff_avg:.1%}' if diff_avg is not None else '—')} | "
            f"{_fmt(pesq_avg, '.2f')} | {_fmt(ovrl_avg, '.2f')} | "
            f"{sum(r['rel_degrade'] for r in subset)} | "
            f"{sum(r['hallucination_score'] for r in subset)} |"
        )
    lines.append("")

    # ── Résumé Cohere vs Whisper ─────────────────────────────────────────────
    lines += ["## Résumé par backend effectif", ""]
    for stt in sorted({r["effective_stt"] for r in rows}):
        subset = [r for r in rows if r["effective_stt"] == stt and r["status"] == "ok"]
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
            f"**{stt}** ({len(subset)} combos OK) :",
            f"- Durée STT moyenne : {avg_stt:.1f}s",
            f"- Durée totale moyenne : {avg_total:.1f}s",
            f"- Segments moyens : {avg_segs:.1f}",
            f"- Mots moyens : {avg_words:.1f}",
            f"- Segments reliability degrade/suspect : "
            f"{sum(r['rel_degrade'] for r in subset)}/{sum(r['rel_suspect'] for r in subset)}",
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
                f"- **{r['combo_id']}** ({r['effective_stt']}, {'no-dia' if r['skip_dia'] else 'dia'}) "
                f"— score={r['hallucination_score']}, "
                f"nsp_high={r['nsp_high']}/{r['n_segs']}, "
                f"nsp_max={_fmt(r['nsp_max'], '.2f')}, "
                f"suspect_segs={r['suspect_segs']}"
                f", risk={r['risk_level'] or '—'}, diff={_fmt(r['difficulty_degrade_ratio'], '.2%')}"
                + (f", overrides: {r['overrides'][:60]}" if r["overrides"] else "")
            )
        lines.append("")

    # ── Relecture qualitative assistée ──────────────────────────────────────
    review_rows = [r for r in rows if r["review_required"]]
    lines += [
        "## Relecture qualitative assistée",
        "",
        "> Ces signaux ne sont pas un verdict automatique. Ils servent uniquement à prioriser",
        "> les SRT à lire manuellement, car les métriques peuvent rater des sorties absurdes",
        "> ou pénaliser des transcriptions pourtant lisibles.",
        "",
    ]
    if review_rows:
        lines += [
            "| Source | ID | STT | raisons | scripts | hits génériques | extraits à relire | verdict manuel | notes |",
            "|--------|----|-----|---------|---------|-----------------|-------------------|----------------|-------|",
        ]
        for r in review_rows:
            snippets = r["review_snippets"][:240] if r["review_snippets"] else "—"
            lines.append(
                f"| {_md_cell(r['source'])} | {_md_cell(r['combo_id'])} | {_md_cell(r['effective_stt'])} | "
                f"{_md_cell(r['review_reasons'] or '—')} | {_md_cell(r['non_latin_scripts'] or '—')} | "
                f"{r['generic_hallucination_hits']} | {_md_cell(snippets)} |  |  |"
            )
    else:
        lines.append("*Aucun signal qualitatif automatique. Lecture humaine toujours recommandée sur un échantillon.*")
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

    if ignored:
        lines += ["## Runs ignorés", ""]
        for item in ignored[:50]:
            source = Path(item.get("_source_dir", ".")).name
            errors = ", ".join(item.get("_schema_errors") or [])
            lines.append(f"- `{source}/{item.get('combo_id') or '?'} `: {errors}")
        if len(ignored) > 50:
            lines.append(f"- … {len(ignored) - 50} autre(s)")
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
        all_results.extend(loaded)

    if not all_results:
        logger.error("Aucun résultat à analyser")
        sys.exit(1)

    supported, ignored = split_supported_results(all_results)
    if ignored:
        logger.warning("%d résultat(s) ignoré(s): contrat JSON trop ancien ou incomplet", len(ignored))
    if not supported:
        logger.error(
            "Aucun résultat compatible schema_version >= %d + audio_corpus.schema_version >= %d",
            MIN_SCHEMA_VERSION,
            MIN_AUDIO_CORPUS_SCHEMA_VERSION,
        )
        sys.exit(1)

    _raw_data = supported
    rows = [analyze_combo(d) for d in supported]

    output_md = args.output or (args.bench_dirs[0] / "analysis.md")
    output_csv = args.csv or (args.bench_dirs[0] / "analysis.csv")

    write_report(rows, output_md, ignored=ignored)
    write_csv(rows, output_csv)

    # Résumé console
    ok = sum(1 for r in rows if r["status"] == "ok")
    hall = sum(1 for r in rows if r["hallucination_score"] > 0)
    cohere_ok = [r for r in rows if r["stt"] == "cohere" and r["status"] == "ok"]
    whisper_ok = [r for r in rows if r["stt"] == "whisper" and r["status"] == "ok"]

    print(f"\n{'='*60}")
    print(f"  {ok}/{len(rows)} combos compatibles OK — {hall} avec hallucinations")
    if ignored:
        print(f"  {len(ignored)} résultat(s) ignoré(s) (schema/audio_corpus manquant)")
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

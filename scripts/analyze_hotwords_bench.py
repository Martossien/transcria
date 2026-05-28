#!/usr/bin/env python3
"""Analyse une campagne Whisper baseline vs hotwords.

Le script agrège les JSON produits par `tests/test_e2e_workflow.py`, regroupe
les paires baseline/hotwords par audio et calcule des deltas simples :
segments, mots, score qualité, warnings, segments suspects et présence des
termes de lexique dans le SRT brut.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logging.basicConfig(
    format="%(asctime)s [analyze_hotwords_bench] %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
    stream=sys.stderr,
)
logger = logging.getLogger("analyze_hotwords_bench")


@dataclass(frozen=True)
class RunMetrics:
    combo_id: str
    variant: str
    audio_path: str
    job_id: str
    job_dir: Path | None
    status: str
    error_count: int
    segments: int
    words: int
    srt_chars: int
    quality_score: int | None
    warnings: int | None
    suspect_segments: int
    degraded_segments: int
    pipeline_s: float | None
    hotwords_injected: int | None
    hotwords_candidates: int | None
    hotwords_tokens: int | None
    hotwords_token_method: str
    lexicon_hits: int


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"JSON illisible: {path} ({exc})") from exc


def _normalize(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(text or "").casefold())
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _variant_and_key(combo_id: str) -> tuple[str, str] | None:
    suffixes = {
        "-whisper-baseline": "baseline",
        "-whisper-hotwords": "hotwords",
    }
    for suffix, variant in suffixes.items():
        if combo_id.endswith(suffix):
            return variant, combo_id[: -len(suffix)]
    return None


def _read_text(path: Path | None) -> str:
    if path is None or not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _word_count(text: str) -> int:
    return len(re.findall(r"(?u)\b[\wÀ-ÿ'-]+\b", text or ""))


def _quality(job_dir: Path | None) -> tuple[int | None, int | None]:
    if job_dir is None:
        return None, None
    path = job_dir / "quality" / "quality_report.json"
    if not path.exists():
        return None, None
    data = _load_json(path)
    return _int_or_none(data.get("quality_score")), _int_or_none(data.get("warnings"))


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _lexicon_terms(path: Path | None) -> list[str]:
    if path is None:
        return []
    data = _load_json(path)
    if not isinstance(data, list):
        return []
    terms: list[str] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        term = str(item.get("replace_by") or item.get("term") or "").strip()
        key = _normalize(term)
        if term and key not in seen:
            seen.add(key)
            terms.append(term)
    return terms


def _count_term_hits(text: str, terms: list[str]) -> int:
    normalized = _normalize(text)
    return sum(1 for term in terms if _normalize(term) in normalized)


def metrics_from_result(path: Path, terms: list[str]) -> RunMetrics | None:
    data = _load_json(path)
    combo_id = str(data.get("combo_id") or "")
    parsed = _variant_and_key(combo_id)
    if parsed is None:
        return None
    variant, _key = parsed
    job_dir = Path(data["job_dir"]) if data.get("job_dir") else None
    srt_path = None
    raw_path = ((data.get("srt") or {}).get("raw_path"))
    if raw_path:
        srt_path = Path(raw_path)
    elif job_dir is not None:
        srt_path = job_dir / "metadata" / "transcription.srt"
    srt_text = _read_text(srt_path)
    reliability = data.get("segment_reliability_counts") or {}
    quality_score, warnings = _quality(job_dir)
    hotwords = data.get("whisper_hotwords_data") or {}
    timings = data.get("timings") or {}
    srt = data.get("srt") or {}
    return RunMetrics(
        combo_id=combo_id,
        variant=variant,
        audio_path=str(data.get("audio_path") or ""),
        job_id=str(data.get("job_id") or ""),
        job_dir=job_dir,
        status=str(data.get("status") or ""),
        error_count=len(data.get("errors") or []),
        segments=int(srt.get("raw_segments") or (data.get("transcription_metadata") or {}).get("segments") or 0),
        words=int(srt.get("raw_words") or _word_count(srt_text)),
        srt_chars=len(srt_text),
        quality_score=quality_score,
        warnings=warnings,
        suspect_segments=int(reliability.get("suspect") or 0),
        degraded_segments=int(reliability.get("degrade") or 0),
        pipeline_s=float(timings["pipeline_s"]) if timings.get("pipeline_s") is not None else None,
        hotwords_injected=_int_or_none(hotwords.get("injected_terms")),
        hotwords_candidates=_int_or_none(hotwords.get("candidate_terms")),
        hotwords_tokens=_int_or_none(hotwords.get("token_count")),
        hotwords_token_method=str(hotwords.get("token_count_method") or ""),
        lexicon_hits=_count_term_hits(srt_text, terms),
    )


def build_report(results_dir: Path, lexicon_json: Path | None) -> dict:
    terms = _lexicon_terms(lexicon_json)
    pairs: dict[str, dict[str, RunMetrics]] = {}
    ignored = 0
    for path in sorted(results_dir.glob("*.json")):
        if path.name in {"manifest.json", "lexicon_from_jobs.json"} or path.name.startswith("smoke-"):
            continue
        metrics = metrics_from_result(path, terms)
        if metrics is None:
            ignored += 1
            continue
        parsed = _variant_and_key(metrics.combo_id)
        assert parsed is not None
        variant, key = parsed
        pairs.setdefault(key, {})[variant] = metrics

    rows = []
    complete_pairs = 0
    for key, variants in sorted(pairs.items()):
        baseline = variants.get("baseline")
        hotwords = variants.get("hotwords")
        if baseline and hotwords:
            complete_pairs += 1
        rows.append(_pair_row(key, baseline, hotwords))

    return {
        "results_dir": str(results_dir),
        "lexicon_json": str(lexicon_json) if lexicon_json else None,
        "lexicon_terms": len(terms),
        "pair_count": len(pairs),
        "complete_pair_count": complete_pairs,
        "ignored_files": ignored,
        "rows": rows,
    }


def _delta(after: int | float | None, before: int | float | None) -> int | float | None:
    if after is None or before is None:
        return None
    return after - before


def _pair_row(key: str, baseline: RunMetrics | None, hotwords: RunMetrics | None) -> dict:
    return {
        "key": key,
        "audio_path": (hotwords or baseline).audio_path if (hotwords or baseline) else "",
        "baseline": _metrics_dict(baseline),
        "hotwords": _metrics_dict(hotwords),
        "delta": {
            "segments": _delta(hotwords.segments if hotwords else None, baseline.segments if baseline else None),
            "words": _delta(hotwords.words if hotwords else None, baseline.words if baseline else None),
            "quality_score": _delta(hotwords.quality_score if hotwords else None, baseline.quality_score if baseline else None),
            "warnings": _delta(hotwords.warnings if hotwords else None, baseline.warnings if baseline else None),
            "suspect_segments": _delta(hotwords.suspect_segments if hotwords else None, baseline.suspect_segments if baseline else None),
            "lexicon_hits": _delta(hotwords.lexicon_hits if hotwords else None, baseline.lexicon_hits if baseline else None),
            "pipeline_s": _delta(hotwords.pipeline_s if hotwords else None, baseline.pipeline_s if baseline else None),
        },
    }


def _metrics_dict(metrics: RunMetrics | None) -> dict | None:
    if metrics is None:
        return None
    return {
        "combo_id": metrics.combo_id,
        "job_id": metrics.job_id,
        "job_dir": str(metrics.job_dir) if metrics.job_dir else "",
        "status": metrics.status,
        "error_count": metrics.error_count,
        "segments": metrics.segments,
        "words": metrics.words,
        "srt_chars": metrics.srt_chars,
        "quality_score": metrics.quality_score,
        "warnings": metrics.warnings,
        "suspect_segments": metrics.suspect_segments,
        "degraded_segments": metrics.degraded_segments,
        "pipeline_s": metrics.pipeline_s,
        "hotwords_injected": metrics.hotwords_injected,
        "hotwords_candidates": metrics.hotwords_candidates,
        "hotwords_tokens": metrics.hotwords_tokens,
        "hotwords_token_method": metrics.hotwords_token_method,
        "lexicon_hits": metrics.lexicon_hits,
    }


def write_markdown(report: dict, output: Path) -> None:
    lines = [
        "# Benchmark Whisper hotwords",
        "",
        f"- Résultats : `{report['results_dir']}`",
        f"- Paires complètes : {report['complete_pair_count']}/{report['pair_count']}",
        f"- Termes lexique analysés : {report['lexicon_terms']}",
        "",
        "| Audio | Score Δ | Warnings Δ | Suspects Δ | Termes Δ | Mots Δ | Segments Δ | Hotwords |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in report["rows"]:
        base = row.get("baseline") or {}
        hot = row.get("hotwords") or {}
        delta = row.get("delta") or {}
        hotwords = ""
        if hot:
            hotwords = f"{hot.get('hotwords_injected')}/{hot.get('hotwords_candidates')} ({hot.get('hotwords_tokens')} tok)"
        lines.append(
            "| {key} | {score} | {warnings} | {suspects} | {terms} | {words} | {segments} | {hotwords} |".format(
                key=row["key"],
                score=_fmt(delta.get("quality_score")),
                warnings=_fmt(delta.get("warnings")),
                suspects=_fmt(delta.get("suspect_segments")),
                terms=_fmt(delta.get("lexicon_hits")),
                words=_fmt(delta.get("words")),
                segments=_fmt(delta.get("segments")),
                hotwords=hotwords,
            )
        )
        if base and hot:
            lines.append(
                f"<!-- {row['key']} baseline_job={base.get('job_id')} hotwords_job={hot.get('job_id')} -->"
            )
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:+.1f}"
    return f"{value:+d}" if isinstance(value, int) else str(value)


def write_csv(report: dict, output: Path) -> None:
    fieldnames = [
        "key",
        "audio_path",
        "baseline_job_id",
        "hotwords_job_id",
        "baseline_score",
        "hotwords_score",
        "delta_score",
        "baseline_warnings",
        "hotwords_warnings",
        "delta_warnings",
        "baseline_suspects",
        "hotwords_suspects",
        "delta_suspects",
        "baseline_lexicon_hits",
        "hotwords_lexicon_hits",
        "delta_lexicon_hits",
        "baseline_words",
        "hotwords_words",
        "delta_words",
        "baseline_segments",
        "hotwords_segments",
        "delta_segments",
        "hotwords_injected",
        "hotwords_tokens",
    ]
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in report["rows"]:
            base = row.get("baseline") or {}
            hot = row.get("hotwords") or {}
            delta = row.get("delta") or {}
            writer.writerow({
                "key": row["key"],
                "audio_path": row.get("audio_path", ""),
                "baseline_job_id": base.get("job_id"),
                "hotwords_job_id": hot.get("job_id"),
                "baseline_score": base.get("quality_score"),
                "hotwords_score": hot.get("quality_score"),
                "delta_score": delta.get("quality_score"),
                "baseline_warnings": base.get("warnings"),
                "hotwords_warnings": hot.get("warnings"),
                "delta_warnings": delta.get("warnings"),
                "baseline_suspects": base.get("suspect_segments"),
                "hotwords_suspects": hot.get("suspect_segments"),
                "delta_suspects": delta.get("suspect_segments"),
                "baseline_lexicon_hits": base.get("lexicon_hits"),
                "hotwords_lexicon_hits": hot.get("lexicon_hits"),
                "delta_lexicon_hits": delta.get("lexicon_hits"),
                "baseline_words": base.get("words"),
                "hotwords_words": hot.get("words"),
                "delta_words": delta.get("words"),
                "baseline_segments": base.get("segments"),
                "hotwords_segments": hot.get("segments"),
                "delta_segments": delta.get("segments"),
                "hotwords_injected": hot.get("hotwords_injected"),
                "hotwords_tokens": hot.get("hotwords_tokens"),
            })


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyse un lot E2E Whisper baseline vs hotwords.")
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--lexicon-json", type=Path)
    parser.add_argument("--output-md", type=Path, required=True)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-csv", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_report(args.results_dir, args.lexicon_json)
    args.output_md.parent.mkdir(parents=True, exist_ok=True)
    write_markdown(report, args.output_md)
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.output_csv:
        args.output_csv.parent.mkdir(parents=True, exist_ok=True)
        write_csv(report, args.output_csv)
    logger.info("Analyse écrite: %s (%d paires complètes)", args.output_md, report["complete_pair_count"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

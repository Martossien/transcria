#!/usr/bin/env python3
"""Compare deux transcriptions segmentées par chevauchement temporel.

Objectif : produire une revue exploitable avant d'implémenter un arbitrage
hybride Cohere/Whisper. Le script ne modifie aucun job et ne décide pas à la
place de l'utilisateur : il aligne les segments, calcule des signaux simples et
propose une recommandation prudente par intervalle.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logging.basicConfig(
    format="%(asctime)s [compare_stt_segments] %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
    stream=sys.stderr,
)
logger = logging.getLogger("compare_stt_segments")


@dataclass(frozen=True)
class Side:
    label: str
    job_id: str
    segments_path: Path
    metadata_path: Path | None = None


@dataclass(frozen=True)
class SegmentView:
    start: float
    end: float
    text: str
    reliability: str
    no_speech_prob: float | None
    low_word_ratio: float | None
    word_count: int
    char_count: int
    source_indices: list[int]


def _job_side(label: str, job_id: str, jobs_dir: Path) -> Side:
    job_dir = jobs_dir / job_id
    return Side(
        label=label,
        job_id=job_id,
        segments_path=job_dir / "metadata" / "transcription_segments.json",
        metadata_path=job_dir / "metadata" / "transcription_metadata.json",
    )


def _file_side(label: str, path: Path) -> Side:
    return Side(label=label, job_id="", segments_path=path)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"JSON illisible: {path} ({exc})") from exc


def load_segments(path: Path) -> list[dict]:
    data = _load_json(path)
    if not isinstance(data, list):
        raise SystemExit(f"Format segments invalide: {path} n'est pas une liste")
    segments: list[dict] = []
    for index, raw in enumerate(data):
        if not isinstance(raw, dict):
            continue
        try:
            start = float(raw.get("start", 0.0))
            end = float(raw.get("end", start))
        except (TypeError, ValueError):
            continue
        text = _clean_text(raw.get("text", ""))
        if end <= start and not text:
            continue
        item = dict(raw)
        item["_index"] = index
        item["start"] = start
        item["end"] = max(start, end)
        item["text"] = text
        segments.append(item)
    return sorted(segments, key=lambda s: (s["start"], s["end"]))


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _words(text: str) -> list[str]:
    return re.findall(r"(?u)\b[\wÀ-ÿ'-]+\b", text)


def _overlap(start: float, end: float, segment: dict) -> float:
    return max(0.0, min(end, float(segment["end"])) - max(start, float(segment["start"])))


def _slice_segments(segments: list[dict], start: float, end: float) -> list[dict]:
    return [segment for segment in segments if _overlap(start, end, segment) > 0.0]


def _low_word_ratio(segments: list[dict]) -> float | None:
    total = 0
    low = 0
    for segment in segments:
        for word in segment.get("words") or []:
            if not isinstance(word, dict):
                continue
            total += 1
            try:
                probability = float(word.get("probability", 1.0))
            except (TypeError, ValueError):
                probability = 1.0
            if probability < 0.4:
                low += 1
    if total == 0:
        return None
    return low / total


def _max_no_speech_prob(segments: list[dict]) -> float | None:
    values: list[float] = []
    for segment in segments:
        value = segment.get("no_speech_prob")
        if value is None:
            continue
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            continue
    return max(values) if values else None


def _worst_reliability(segments: list[dict]) -> str:
    order = {"ok": 0, "suspect": 1, "degrade": 2}
    worst = "ok"
    for segment in segments:
        reliability = str(segment.get("reliability") or "ok")
        if order.get(reliability, 0) > order.get(worst, 0):
            worst = reliability
    return worst


def _view_for(start: float, end: float, segments: list[dict]) -> SegmentView:
    text = _clean_text(" ".join(_clean_text(segment.get("text", "")) for segment in segments))
    words = _words(text)
    return SegmentView(
        start=start,
        end=end,
        text=text,
        reliability=_worst_reliability(segments),
        no_speech_prob=_max_no_speech_prob(segments),
        low_word_ratio=_low_word_ratio(segments),
        word_count=len(words),
        char_count=len(text),
        source_indices=[int(segment.get("_index", -1)) for segment in segments],
    )


def _timeline(left: list[dict], right: list[dict], min_interval_s: float) -> list[tuple[float, float]]:
    points = sorted({float(s["start"]) for s in left + right} | {float(s["end"]) for s in left + right})
    intervals: list[tuple[float, float]] = []
    for start, end in zip(points, points[1:]):
        if end - start < min_interval_s:
            continue
        if _slice_segments(left, start, end) or _slice_segments(right, start, end):
            intervals.append((start, end))
    return intervals


def _load_lexicon_terms(paths: list[Path]) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for path in paths:
        data = _load_json(path)
        if not isinstance(data, list):
            logger.warning("Lexique ignoré, format inattendu: %s", path)
            continue
        for item in data:
            if not isinstance(item, dict):
                continue
            priority = str(item.get("priority") or "normale")
            if priority not in {"critique", "importante"}:
                continue
            term = _clean_text(item.get("replace_by") or item.get("term"))
            key = _normalize(term)
            if term and key not in seen:
                seen.add(key)
                terms.append(term)
    return terms


def _normalize(text: str) -> str:
    import unicodedata

    normalized = unicodedata.normalize("NFKD", str(text or "").casefold())
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _term_hits(text: str, terms: list[str]) -> list[str]:
    haystack = _normalize(text)
    hits: list[str] = []
    for term in terms:
        if _normalize(term) in haystack:
            hits.append(term)
    return hits


def _score(view: SegmentView, term_hits: list[str]) -> int:
    score = 0
    if view.text:
        score += 2
    if view.reliability == "ok":
        score += 3
    elif view.reliability == "suspect":
        score += 1
    else:
        score -= 2
    if view.no_speech_prob is not None:
        if view.no_speech_prob > 0.8:
            score -= 3
        elif view.no_speech_prob > 0.5:
            score -= 1
    if view.low_word_ratio is not None:
        if view.low_word_ratio > 0.5:
            score -= 3
        elif view.low_word_ratio > 0.2:
            score -= 1
    if view.word_count <= 1 and view.char_count <= 4:
        score -= 1
    if view.word_count > 0:
        score += min(2, len(term_hits))
    return score


def _recommend(left: SegmentView, right: SegmentView, left_terms: list[str], right_terms: list[str], left_label: str, right_label: str) -> tuple[str, str]:
    left_score = _score(left, left_terms)
    right_score = _score(right, right_terms)
    delta = right_score - left_score
    if abs(delta) < 2:
        return "review", f"scores proches {left_label}={left_score}, {right_label}={right_score}"
    if delta > 0:
        return right_label, f"{right_label} score {right_score} > {left_label} {left_score}"
    return left_label, f"{left_label} score {left_score} > {right_label} {right_score}"


def build_comparison(left_side: Side, right_side: Side, lexicon_terms: list[str], min_interval_s: float) -> dict:
    left_segments = load_segments(left_side.segments_path)
    right_segments = load_segments(right_side.segments_path)
    intervals = _timeline(left_segments, right_segments, min_interval_s)
    rows: list[dict] = []
    counts: dict[str, int] = {left_side.label: 0, right_side.label: 0, "review": 0}

    for start, end in intervals:
        left_view = _view_for(start, end, _slice_segments(left_segments, start, end))
        right_view = _view_for(start, end, _slice_segments(right_segments, start, end))
        left_hits = _term_hits(left_view.text, lexicon_terms)
        right_hits = _term_hits(right_view.text, lexicon_terms)
        recommendation, reason = _recommend(left_view, right_view, left_hits, right_hits, left_side.label, right_side.label)
        counts[recommendation] = counts.get(recommendation, 0) + 1
        rows.append({
            "start": round(start, 3),
            "end": round(end, 3),
            "duration_s": round(end - start, 3),
            "recommendation": recommendation,
            "reason": reason,
            left_side.label: _view_to_dict(left_view, left_hits),
            right_side.label: _view_to_dict(right_view, right_hits),
        })

    return {
        "left": _side_info(left_side, left_segments),
        "right": _side_info(right_side, right_segments),
        "lexicon_terms": lexicon_terms,
        "interval_count": len(rows),
        "recommendation_counts": counts,
        "rows": rows,
    }


def _view_to_dict(view: SegmentView, term_hits: list[str]) -> dict:
    return {
        "text": view.text,
        "reliability": view.reliability,
        "no_speech_prob": round(view.no_speech_prob, 3) if view.no_speech_prob is not None else None,
        "low_word_ratio": round(view.low_word_ratio, 3) if view.low_word_ratio is not None else None,
        "word_count": view.word_count,
        "char_count": view.char_count,
        "source_indices": view.source_indices,
        "term_hits": term_hits,
    }


def _side_info(side: Side, segments: list[dict]) -> dict:
    metadata = {}
    if side.metadata_path and side.metadata_path.exists():
        try:
            metadata = _load_json(side.metadata_path)
        except SystemExit:
            metadata = {}
    return {
        "label": side.label,
        "job_id": side.job_id,
        "segments_path": str(side.segments_path),
        "segment_count": len(segments),
        "metadata": metadata,
    }


def write_markdown(report: dict, output: Path, max_rows: int) -> None:
    left_label = report["left"]["label"]
    right_label = report["right"]["label"]
    lines = [
        "# Comparaison segmentaire STT",
        "",
        f"- Gauche : `{left_label}` ({report['left']['segment_count']} segments)",
        f"- Droite : `{right_label}` ({report['right']['segment_count']} segments)",
        f"- Intervalles alignés : {report['interval_count']}",
        f"- Recommandations : {report['recommendation_counts']}",
        "",
        "## Lecture",
        "",
        "La recommandation est heuristique. `review` signifie que les signaux sont trop proches ou ambigus ; ce sont les meilleurs candidats pour une relecture humaine ou un arbitrage LLM limité.",
        "",
        "## Intervalles",
        "",
    ]
    for row in report["rows"][:max_rows]:
        lines.extend(_row_markdown(row, left_label, right_label))
    if len(report["rows"]) > max_rows:
        lines += ["", f"_Rapport tronqué à {max_rows}/{len(report['rows'])} intervalles._", ""]
    output.write_text("\n".join(lines), encoding="utf-8")


def _row_markdown(row: dict, left_label: str, right_label: str) -> list[str]:
    left = row[left_label]
    right = row[right_label]
    return [
        f"### {row['start']:.1f}s → {row['end']:.1f}s · `{row['recommendation']}`",
        "",
        f"Raison : {row['reason']}",
        "",
        f"**{left_label}** · rel={left['reliability']} nsp={left['no_speech_prob']} low={left['low_word_ratio']} termes={left['term_hits']}",
        "",
        f"> {_clip(left['text']) or '(vide)'}",
        "",
        f"**{right_label}** · rel={right['reliability']} nsp={right['no_speech_prob']} low={right['low_word_ratio']} termes={right['term_hits']}",
        "",
        f"> {_clip(right['text']) or '(vide)'}",
        "",
    ]


def _clip(text: str, max_chars: int = 700) -> str:
    text = _clean_text(text)
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0] + "..."


def write_csv(report: dict, output: Path) -> None:
    left_label = report["left"]["label"]
    right_label = report["right"]["label"]
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "start",
                "end",
                "duration_s",
                "recommendation",
                "reason",
                f"{left_label}_reliability",
                f"{left_label}_no_speech_prob",
                f"{left_label}_text",
                f"{right_label}_reliability",
                f"{right_label}_no_speech_prob",
                f"{right_label}_text",
            ],
        )
        writer.writeheader()
        for row in report["rows"]:
            left = row[left_label]
            right = row[right_label]
            writer.writerow({
                "start": row["start"],
                "end": row["end"],
                "duration_s": row["duration_s"],
                "recommendation": row["recommendation"],
                "reason": row["reason"],
                f"{left_label}_reliability": left["reliability"],
                f"{left_label}_no_speech_prob": left["no_speech_prob"],
                f"{left_label}_text": left["text"],
                f"{right_label}_reliability": right["reliability"],
                f"{right_label}_no_speech_prob": right["no_speech_prob"],
                f"{right_label}_text": right["text"],
            })


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare deux fichiers transcription_segments.json par timecode.")
    parser.add_argument("--jobs-dir", type=Path, default=Path("jobs"))
    parser.add_argument("--left-job", help="Job ID côté gauche")
    parser.add_argument("--right-job", help="Job ID côté droit")
    parser.add_argument("--left-segments", type=Path, help="Fichier transcription_segments.json côté gauche")
    parser.add_argument("--right-segments", type=Path, help="Fichier transcription_segments.json côté droit")
    parser.add_argument("--left-label", default="cohere")
    parser.add_argument("--right-label", default="whisper")
    parser.add_argument("--lexicon-json", type=Path, action="append", default=[])
    parser.add_argument("--min-interval-s", type=float, default=0.25)
    parser.add_argument("--output-md", type=Path, required=True)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-csv", type=Path)
    parser.add_argument("--max-md-rows", type=int, default=120)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if bool(args.left_job) == bool(args.left_segments):
        raise SystemExit("Indique exactement un --left-job ou --left-segments")
    if bool(args.right_job) == bool(args.right_segments):
        raise SystemExit("Indique exactement un --right-job ou --right-segments")

    left = _job_side(args.left_label, args.left_job, args.jobs_dir) if args.left_job else _file_side(args.left_label, args.left_segments)
    right = _job_side(args.right_label, args.right_job, args.jobs_dir) if args.right_job else _file_side(args.right_label, args.right_segments)
    lexicon_terms = _load_lexicon_terms(args.lexicon_json)

    report = build_comparison(left, right, lexicon_terms, args.min_interval_s)
    args.output_md.parent.mkdir(parents=True, exist_ok=True)
    write_markdown(report, args.output_md, args.max_md_rows)
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.output_csv:
        args.output_csv.parent.mkdir(parents=True, exist_ok=True)
        write_csv(report, args.output_csv)

    logger.info(
        "Comparaison écrite: %s (%d intervalles, recommandations=%s)",
        args.output_md,
        report["interval_count"],
        report["recommendation_counts"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

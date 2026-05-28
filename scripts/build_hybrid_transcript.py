#!/usr/bin/env python3
"""Construit un prototype de transcription hybride depuis plusieurs jobs STT.

Le script est volontairement hors workflow applicatif. Il sert à tester des
règles d'arbitrage Cohere / Whisper / Whisper hotwords avec une sortie SRT et
un JSON d'audit avant toute intégration produit.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logging.basicConfig(
    format="%(asctime)s [build_hybrid_transcript] %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
    stream=sys.stderr,
)
logger = logging.getLogger("build_hybrid_transcript")

GENERIC_HALLUCINATION_PATTERNS = [
    re.compile(r"\bpour plus d['’]informations\b", re.IGNORECASE),
    re.compile(r"\bcontactez[- ]?nous\b", re.IGNORECASE),
    re.compile(r"\babonnez[- ]?vous\b", re.IGNORECASE),
    re.compile(r"\bsite web\b", re.IGNORECASE),
    re.compile(r"\buniversit[eé] d['’]ottawa\b", re.IGNORECASE),
    re.compile(r"\bsous[- ]?titrage\b", re.IGNORECASE),
]


@dataclass(frozen=True)
class CandidateSource:
    label: str
    job_id: str
    segments_path: Path


@dataclass(frozen=True)
class CandidateWindow:
    label: str
    text: str
    reliability: str
    no_speech_prob: float | None
    low_word_ratio: float | None
    source_indices: list[int]
    segment_count: int
    word_count: int
    char_count: int
    term_hits: list[str]
    generic_hallucinations: list[str]


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _words(text: str) -> list[str]:
    return re.findall(r"(?u)\b[\wÀ-ÿ'-]+\b", text)


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
    return sorted(segments, key=lambda segment: (segment["start"], segment["end"]))


def _overlap(start: float, end: float, segment: dict) -> float:
    return max(0.0, min(end, float(segment["end"])) - max(start, float(segment["start"])))


def _slice_segments(segments: list[dict], start: float, end: float) -> list[dict]:
    return [segment for segment in segments if _overlap(start, end, segment) > 0.0]


def _normalize(text: str) -> str:
    import unicodedata

    normalized = unicodedata.normalize("NFKD", str(text or "").casefold())
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


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
            for value in [item.get("replace_by") or item.get("term"), *(item.get("variants") or [])]:
                term = _clean_text(value)
                key = _normalize(term)
                if term and key not in seen:
                    seen.add(key)
                    terms.append(term)
    return terms


def _term_hits(text: str, terms: list[str]) -> list[str]:
    haystack = _normalize(text)
    hits: list[str] = []
    for term in terms:
        normalized_term = _normalize(term)
        if not normalized_term:
            continue
        pattern = re.compile(rf"(?<![\wÀ-ÿ]){re.escape(normalized_term)}(?![\wÀ-ÿ])", re.IGNORECASE)
        if pattern.search(haystack):
            hits.append(term)
    return hits


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


def _generic_hallucinations(text: str) -> list[str]:
    matches: list[str] = []
    for pattern in GENERIC_HALLUCINATION_PATTERNS:
        match = pattern.search(text)
        if match:
            matches.append(match.group(0))
    return matches


def _window_view(label: str, start: float, end: float, segments: list[dict], lexicon_terms: list[str]) -> CandidateWindow:
    selected = _slice_segments(segments, start, end)
    text = _clean_text(" ".join(_clean_text(segment.get("text", "")) for segment in selected))
    words = _words(text)
    return CandidateWindow(
        label=label,
        text=text,
        reliability=_worst_reliability(selected),
        no_speech_prob=_max_no_speech_prob(selected),
        low_word_ratio=_low_word_ratio(selected),
        source_indices=[int(segment.get("_index", -1)) for segment in selected],
        segment_count=len(selected),
        word_count=len(words),
        char_count=len(text),
        term_hits=_term_hits(text, lexicon_terms),
        generic_hallucinations=_generic_hallucinations(text),
    )


def build_windows(all_segments: list[list[dict]], window_s: float) -> list[tuple[float, float]]:
    starts = [float(segment["start"]) for segments in all_segments for segment in segments]
    ends = [float(segment["end"]) for segments in all_segments for segment in segments]
    if not starts or not ends:
        return []
    current = math.floor(min(starts) / window_s) * window_s
    last = math.ceil(max(ends) / window_s) * window_s
    windows: list[tuple[float, float]] = []
    while current < last:
        start = current
        end = min(current + window_s, last)
        if any(_slice_segments(segments, start, end) for segments in all_segments):
            windows.append((round(start, 3), round(end, 3)))
        current += window_s
    return windows


def _score_candidate(view: CandidateWindow, window_duration_s: float) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    words_per_second = view.word_count / max(window_duration_s, 0.001)

    if view.text:
        score += 4
        reasons.append("texte présent")
    else:
        score -= 8
        reasons.append("texte vide")

    if view.reliability == "ok":
        score += 3
        reasons.append("fiabilité ok")
    elif view.reliability == "suspect":
        score -= 1
        reasons.append("segment suspect")
    else:
        score -= 5
        reasons.append("segment dégradé")

    if view.no_speech_prob is not None:
        if view.no_speech_prob > 0.8:
            score -= 5
            reasons.append(f"no_speech élevé {view.no_speech_prob:.2f}")
        elif view.no_speech_prob > 0.5:
            score -= 2
            reasons.append(f"no_speech moyen {view.no_speech_prob:.2f}")

    if view.low_word_ratio is not None:
        if view.low_word_ratio > 0.5:
            score -= 4
            reasons.append(f"confiance mots faible {view.low_word_ratio:.0%}")
        elif view.low_word_ratio > 0.25:
            score -= 2
            reasons.append(f"confiance mots mitigée {view.low_word_ratio:.0%}")

    if view.generic_hallucinations:
        score -= 10
        reasons.append("hallucination générique détectée")

    if view.word_count <= 2:
        score -= 2
        reasons.append("texte très court")
    elif 0.7 <= words_per_second <= 4.2:
        score += 2
        reasons.append("densité parole plausible")
    elif words_per_second > 5.0:
        score -= 3
        reasons.append("densité texte trop forte")

    if view.term_hits:
        bonus = min(4, len(view.term_hits) * 2)
        score += bonus
        reasons.append(f"termes lexique: {', '.join(view.term_hits[:4])}")

    if view.segment_count > 12 and window_duration_s <= 30:
        score -= 1
        reasons.append("fragmentation élevée")

    return score, reasons


def choose_window(candidates: list[CandidateWindow], start: float, end: float, margin: int) -> dict:
    duration = end - start
    scored: list[dict] = []
    for candidate in candidates:
        score, reasons = _score_candidate(candidate, duration)
        scored.append({
            "label": candidate.label,
            "score": score,
            "reasons": reasons,
            "text": candidate.text,
            "reliability": candidate.reliability,
            "no_speech_prob": round(candidate.no_speech_prob, 3) if candidate.no_speech_prob is not None else None,
            "low_word_ratio": round(candidate.low_word_ratio, 3) if candidate.low_word_ratio is not None else None,
            "word_count": candidate.word_count,
            "segment_count": candidate.segment_count,
            "term_hits": candidate.term_hits,
            "generic_hallucinations": candidate.generic_hallucinations,
            "source_indices": candidate.source_indices,
        })

    scored.sort(key=lambda item: (item["score"], item["word_count"]), reverse=True)
    best = scored[0] if scored else None
    second = scored[1] if len(scored) > 1 else None
    decision = "review"
    selected_label = best["label"] if best else "review"
    selected_text = best["text"] if best else ""
    reason = "aucun candidat exploitable"

    if best:
        if best["score"] < 1:
            reason = f"meilleur score trop faible ({best['label']}={best['score']})"
        elif best["reliability"] != "ok" and not best["term_hits"]:
            reason = f"meilleur candidat non fiable sans terme métier ({best['label']} rel={best['reliability']})"
        elif second and best["score"] - second["score"] < margin:
            reason = f"scores proches {best['label']}={best['score']}, {second['label']}={second['score']}"
        else:
            decision = best["label"]
            reason = f"{best['label']} retenu avec marge suffisante"

    return {
        "start": round(start, 3),
        "end": round(end, 3),
        "duration_s": round(duration, 3),
        "decision": decision,
        "selected_label": selected_label,
        "selected_text": selected_text,
        "reason": reason,
        "candidates": scored,
    }


def build_hybrid_report(sources: list[CandidateSource], lexicon_terms: list[str], window_s: float, decision_margin: int) -> dict:
    loaded = [(source, load_segments(source.segments_path)) for source in sources]
    windows = build_windows([segments for _, segments in loaded], window_s)
    rows: list[dict] = []
    counts: dict[str, int] = {"review": 0}
    for source in sources:
        counts[source.label] = 0

    for start, end in windows:
        candidate_views = [
            _window_view(source.label, start, end, segments, lexicon_terms)
            for source, segments in loaded
        ]
        row = choose_window(candidate_views, start, end, decision_margin)
        counts[row["decision"]] = counts.get(row["decision"], 0) + 1
        rows.append(row)

    return {
        "tool": "build_hybrid_transcript",
        "version": 1,
        "window_s": window_s,
        "decision_margin": decision_margin,
        "sources": [
            {
                "label": source.label,
                "job_id": source.job_id,
                "segments_path": str(source.segments_path),
                "segment_count": len(segments),
            }
            for source, segments in loaded
        ],
        "lexicon_terms": lexicon_terms,
        "window_count": len(rows),
        "decision_counts": counts,
        "windows": rows,
    }


def _split_sentences(text: str) -> list[str]:
    text = _clean_text(text)
    if not text:
        return []
    pieces = re.split(r"(?<=[.!?…])\s+", text)
    return [piece.strip() for piece in pieces if piece.strip()]


def split_text_for_srt(text: str, max_words: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    for sentence in _split_sentences(text):
        words = _words(sentence)
        if not words:
            continue
        if len(words) > max_words:
            if current:
                chunks.append(_clean_text(" ".join(current)))
                current = []
            raw_words = sentence.split()
            for index in range(0, len(raw_words), max_words):
                chunks.append(_clean_text(" ".join(raw_words[index:index + max_words])))
            continue
        current_words = sum(len(_words(item)) for item in current)
        if current and current_words + len(words) > max_words:
            chunks.append(_clean_text(" ".join(current)))
            current = [sentence]
        else:
            current.append(sentence)
    if current:
        chunks.append(_clean_text(" ".join(current)))
    return chunks


def _format_time(seconds: float) -> str:
    milliseconds = int(round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def write_srt(report: dict, output: Path, max_words: int) -> None:
    index = 1
    lines: list[str] = []
    for window in report["windows"]:
        chunks = split_text_for_srt(window["selected_text"], max_words=max_words)
        if not chunks:
            continue
        start = float(window["start"])
        end = float(window["end"])
        duration = max(0.5, end - start)
        chunk_duration = duration / len(chunks)
        for offset, chunk in enumerate(chunks):
            cue_start = start + offset * chunk_duration
            cue_end = min(end, cue_start + chunk_duration)
            lines.extend([
                str(index),
                f"{_format_time(cue_start)} --> {_format_time(cue_end)}",
                chunk,
                "",
            ])
            index += 1
    output.write_text("\n".join(lines), encoding="utf-8")


def write_markdown(report: dict, output: Path, max_rows: int) -> None:
    lines = [
        "# Prototype transcription hybride",
        "",
        f"- Fenêtres : {report['window_count']} x {report['window_s']}s",
        f"- Décisions : {report['decision_counts']}",
        "",
        "## Lecture",
        "",
        "`review` est volontairement conservateur : le SRT de sortie utilise le meilleur candidat, mais le JSON d'audit signale que la décision ne doit pas être automatisée sans relecture.",
        "",
        "## Fenêtres",
        "",
    ]
    for window in report["windows"][:max_rows]:
        lines.extend([
            f"### {window['start']:.1f}s → {window['end']:.1f}s · `{window['decision']}`",
            "",
            f"Choix SRT : `{window['selected_label']}`. Raison : {window['reason']}",
            "",
        ])
        for candidate in window["candidates"]:
            text = candidate["text"]
            if len(text) > 500:
                text = text[:500].rsplit(" ", 1)[0] + "..."
            lines.extend([
                f"**{candidate['label']}** · score={candidate['score']} rel={candidate['reliability']} mots={candidate['word_count']} termes={candidate['term_hits']} alertes={candidate['generic_hallucinations']}",
                "",
                f"> {text or '(vide)'}",
                "",
            ])
    if len(report["windows"]) > max_rows:
        lines.append(f"_Rapport tronqué à {max_rows}/{len(report['windows'])} fenêtres._")
    output.write_text("\n".join(lines), encoding="utf-8")


def _parse_candidate(value: str, jobs_dir: Path) -> CandidateSource:
    if "=" not in value:
        raise SystemExit("--candidate doit utiliser le format label=job_id ou label=/chemin/segments.json")
    label, raw = value.split("=", 1)
    label = _clean_text(label)
    raw = _clean_text(raw)
    if not label or not raw:
        raise SystemExit("--candidate contient un label ou une valeur vide")
    path = Path(raw)
    if path.exists():
        return CandidateSource(label=label, job_id="", segments_path=path)
    return CandidateSource(
        label=label,
        job_id=raw,
        segments_path=jobs_dir / raw / "metadata" / "transcription_segments.json",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Construit un SRT hybride depuis plusieurs transcriptions STT.")
    parser.add_argument("--jobs-dir", type=Path, default=Path("jobs"))
    parser.add_argument("--candidate", action="append", required=True, help="Format label=job_id ou label=/path/transcription_segments.json. Répéter 2+ fois.")
    parser.add_argument("--lexicon-json", type=Path, action="append", default=[])
    parser.add_argument("--window-s", type=float, default=30.0)
    parser.add_argument("--decision-margin", type=int, default=3)
    parser.add_argument("--max-srt-words", type=int, default=18)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-srt", type=Path, required=True)
    parser.add_argument("--output-md", type=Path)
    parser.add_argument("--max-md-rows", type=int, default=80)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.window_s <= 0:
        raise SystemExit("--window-s doit être positif")
    if args.max_srt_words <= 0:
        raise SystemExit("--max-srt-words doit être positif")

    sources = [_parse_candidate(value, args.jobs_dir) for value in args.candidate]
    if len(sources) < 2:
        raise SystemExit("Au moins deux --candidate sont nécessaires")

    for source in sources:
        if not source.segments_path.exists():
            raise SystemExit(f"Segments introuvables pour {source.label}: {source.segments_path}")

    lexicon_terms = _load_lexicon_terms(args.lexicon_json)
    report = build_hybrid_report(sources, lexicon_terms, args.window_s, args.decision_margin)

    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_srt.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_srt(report, args.output_srt, args.max_srt_words)
    if args.output_md:
        args.output_md.parent.mkdir(parents=True, exist_ok=True)
        write_markdown(report, args.output_md, args.max_md_rows)

    logger.info(
        "SRT hybride écrit: %s (%d fenêtres, décisions=%s)",
        args.output_srt,
        report["window_count"],
        report["decision_counts"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Score un bench STT contre des fenêtres de référence.

Ce score est un proxy de calibration : le DOCX marché n'est pas forcément une
vérité parfaite, mais il donne enfin un repère textuel stable. Le rapport expose
WER/CER approximatifs et ratio de mots ; il ne remplace pas la lecture humaine.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import unicodedata
from pathlib import Path

RESULT_PATTERNS = ["[0-9][0-9][0-9].json", "E[0-9][0-9].json", "S[0-9][0-9].json", "V[0-9][0-9].json"]


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"\b(?:speaker|intervenant)[_\s-]*\d+\b", " ", text)
    text = re.sub(r"\d+\s*\n\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}", " ", text)
    text = re.sub(r"[^\w\s'-]+", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def words(text: str) -> list[str]:
    normalized = normalize_text(text)
    if not normalized:
        return []
    return normalized.split()


def levenshtein(a: list[str] | str, b: list[str] | str) -> int:
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (0 if ca == cb else 1)))
        previous = current
    return previous[-1]


def srt_text(raw_srt: str) -> str:
    lines = []
    for line in raw_srt.splitlines():
        stripped = line.strip()
        if not stripped or stripped.isdigit() or "-->" in stripped:
            continue
        if ":" in stripped:
            prefix, rest = stripped.split(":", 1)
            if re.match(r"^(?:SPEAKER|INTERVENANT)[_\s-]*\d+(?:\([^)]*\))?$", prefix.strip(), re.IGNORECASE):
                stripped = rest.strip()
        lines.append(stripped)
    return " ".join(lines)


def reference_text(reference_json: Path) -> str:
    data = json.loads(reference_json.read_text(encoding="utf-8"))
    return " ".join(segment.get("text") or "" for segment in data.get("segments") or [])


def load_results(bench_dir: Path) -> list[dict]:
    results = []
    seen = set()
    for pattern in RESULT_PATTERNS:
        for path in sorted(bench_dir.glob(pattern)):
            if path in seen:
                continue
            seen.add(path)
            data = json.loads(path.read_text(encoding="utf-8"))
            data["_path"] = str(path)
            data["_bench_dir"] = str(bench_dir)
            results.append(data)
    return results


def score_pair(reference: str, hypothesis: str) -> dict:
    ref_words = words(reference)
    hyp_words = words(hypothesis)
    ref_norm = normalize_text(reference)
    hyp_norm = normalize_text(hypothesis)
    wer_distance = levenshtein(ref_words, hyp_words)
    cer_distance = levenshtein(ref_norm, hyp_norm)
    return {
        "ref_words": len(ref_words),
        "hyp_words": len(hyp_words),
        "word_ratio": round(len(hyp_words) / len(ref_words), 4) if ref_words else None,
        "wer": round(wer_distance / len(ref_words), 4) if ref_words else None,
        "cer": round(cer_distance / len(ref_norm), 4) if ref_norm else None,
    }


def score_windows(windows_dir: Path, bench_root: Path) -> list[dict]:
    rows = []
    for window_dir in sorted(path for path in windows_dir.iterdir() if path.is_dir()):
        reference_json = window_dir / "reference.json"
        bench_dir = bench_root / window_dir.name
        if not reference_json.is_file() or not bench_dir.is_dir():
            continue
        ref_text = reference_text(reference_json)
        ref_data = json.loads(reference_json.read_text(encoding="utf-8"))
        for result in load_results(bench_dir):
            srt = result.get("srt") or {}
            hyp_text = srt_text(srt.get("raw_content") or "")
            scores = score_pair(ref_text, hyp_text)
            rows.append({
                "window": window_dir.name,
                "combo_id": result.get("combo_id") or "",
                "stt_backend": result.get("effective_stt_backend") or result.get("stt_backend") or "",
                "status": result.get("status") or "",
                "reference_speakers": ref_data.get("speaker_count"),
                "reference_segments": ref_data.get("segment_count"),
                **scores,
                "total_s": result.get("_elapsed_wall_s") or "",
                "vram_peak_mb": result.get("vram_peak_mb") or "",
                "result_path": result.get("_path") or "",
            })
    return rows


def write_csv(rows: list[dict], output: Path) -> None:
    if not rows:
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _fmt(value, digits: int = 3) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def write_report(rows: list[dict], output: Path) -> None:
    lines = [
        "# Score référence STT",
        "",
        "> WER/CER approximatifs contre le DOCX de référence. Le DOCX marché reste un proxy, pas une vérité parfaite.",
        "",
        "| Fenêtre | STT | status | ref mots | hyp mots | ratio | WER | CER | temps | VRAM |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row['window']} | {row['stt_backend']} | {row['status']} | {row['ref_words']} | {row['hyp_words']} | "
            f"{_fmt(row['word_ratio'])} | {_fmt(row['wer'])} | {_fmt(row['cer'])} | "
            f"{_fmt(row['total_s'], 1)} | {_fmt(row['vram_peak_mb'], 0)} |"
        )

    lines += ["", "## Moyennes par backend", ""]
    for backend in sorted({row["stt_backend"] for row in rows}):
        subset = [row for row in rows if row["stt_backend"] == backend and row["status"] == "ok"]
        if not subset:
            continue
        lines += [
            f"**{backend}** ({len(subset)} fenêtres OK)",
            f"- WER moyen : {sum(row['wer'] for row in subset if row['wer'] is not None) / len(subset):.3f}",
            f"- CER moyen : {sum(row['cer'] for row in subset if row['cer'] is not None) / len(subset):.3f}",
            f"- Ratio mots moyen : {sum(row['word_ratio'] for row in subset if row['word_ratio'] is not None) / len(subset):.3f}",
            f"- Temps moyen : {sum(float(row['total_s'] or 0.0) for row in subset) / len(subset):.1f}s",
            "",
        ]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Score un bench STT contre des fenêtres de référence.")
    parser.add_argument("--windows-dir", type=Path, required=True)
    parser.add_argument("--bench-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--csv", type=Path, required=True)
    args = parser.parse_args()

    rows = score_windows(args.windows_dir, args.bench_root)
    if not rows:
        raise SystemExit("Aucun résultat scoré")
    write_csv(rows, args.csv)
    write_report(rows, args.output)
    print(f"rows={len(rows)} report={args.output} csv={args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

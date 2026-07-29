#!/usr/bin/env python3
"""Prépare des fenêtres audio + référence à partir d'une longue réunion.

Entrées :
  - audio long
  - reference.json produit par extract_reference_docx.py
  - fenêtres HH:MM:SS-HH:MM:SS

Sorties par fenêtre :
  - audio WAV mono 16 kHz
  - reference.json avec timestamps relatifs à la fenêtre
  - reference.srt relatif à la fenêtre
  - manifest.json global
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

from extract_reference_docx import seconds_to_srt_time

WINDOW_RE = re.compile(r"^(?P<start>\d{1,2}:\d{2}:\d{2})-(?P<end>\d{1,2}:\d{2}:\d{2})$")


def hms_to_seconds(value: str) -> float:
    parts = value.split(":")
    if len(parts) != 3:
        raise ValueError(f"Timestamp invalide: {value!r}")
    hours, minutes, seconds = (int(part) for part in parts)
    return float(hours * 3600 + minutes * 60 + seconds)


def seconds_to_hms(seconds: float) -> str:
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def parse_window(value: str) -> tuple[float, float]:
    match = WINDOW_RE.match(value.strip())
    if not match:
        raise argparse.ArgumentTypeError(f"Fenêtre invalide: {value!r}; format attendu HH:MM:SS-HH:MM:SS")
    start = hms_to_seconds(match.group("start"))
    end = hms_to_seconds(match.group("end"))
    if end <= start:
        raise argparse.ArgumentTypeError(f"Fenêtre invalide: fin <= début ({value!r})")
    return start, end


def window_slug(index: int, start: float, end: float) -> str:
    return f"W{index:02d}_{seconds_to_hms(start).replace(':', '')}_{seconds_to_hms(end).replace(':', '')}"


def clip_reference(reference: dict, start: float, end: float) -> dict:
    clipped = []
    for segment in reference.get("segments") or []:
        seg_start = float(segment["start"])
        seg_end = float(segment["end"])
        if seg_end <= start or seg_start >= end:
            continue
        item = dict(segment)
        item["absolute_start"] = seg_start
        item["absolute_end"] = seg_end
        item["start"] = max(seg_start, start) - start
        item["end"] = min(seg_end, end) - start
        item["duration"] = max(item["end"] - item["start"], 0.0)
        clipped.append(item)

    speakers = sorted({segment["speaker"] for segment in clipped})
    return {
        "schema_version": 1,
        "source_reference": reference.get("source_docx") or reference.get("source_reference"),
        "absolute_start_s": start,
        "absolute_end_s": end,
        "duration_s": end - start,
        "segment_count": len(clipped),
        "speaker_count": len(speakers),
        "speakers": speakers,
        "declared_word_count": sum(segment.get("declared_words") or 0 for segment in clipped),
        "computed_word_count": sum(segment.get("word_count") or 0 for segment in clipped),
        "segments": clipped,
    }


def write_srt(reference: dict, output_path: Path) -> None:
    blocks = []
    for idx, segment in enumerate(reference.get("segments") or [], start=1):
        blocks.append(
            "\n".join(
                [
                    str(idx),
                    f"{seconds_to_srt_time(float(segment['start']))} --> {seconds_to_srt_time(float(segment['end']))}",
                    f"{segment.get('speaker') or 'INTERVENANT'}: {segment.get('text') or ''}",
                ]
            )
        )
    output_path.write_text("\n\n".join(blocks) + ("\n" if blocks else ""), encoding="utf-8")


def write_audio_window(audio_path: Path, output_path: Path, start: float, end: float) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        seconds_to_hms(start),
        "-t",
        seconds_to_hms(end - start),
        "-i",
        str(audio_path),
        "-ac",
        "1",
        "-ar",
        "16000",
        str(output_path),
    ]
    subprocess.run(cmd, check=True)


def prepare_windows(audio_path: Path, reference_path: Path, output_dir: Path, windows: list[tuple[float, float]]) -> dict:
    reference = json.loads(reference_path.read_text(encoding="utf-8"))
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "audio_path": str(audio_path),
        "reference_path": str(reference_path),
        "output_dir": str(output_dir),
        "windows": [],
    }
    for idx, (start, end) in enumerate(windows, start=1):
        slug = window_slug(idx, start, end)
        window_dir = output_dir / slug
        window_dir.mkdir(parents=True, exist_ok=True)
        audio_out = window_dir / f"{slug}.wav"
        reference_json = window_dir / "reference.json"
        reference_srt = window_dir / "reference.srt"

        write_audio_window(audio_path, audio_out, start, end)
        clipped = clip_reference(reference, start, end)
        reference_json.write_text(json.dumps(clipped, indent=2, ensure_ascii=False), encoding="utf-8")
        write_srt(clipped, reference_srt)

        manifest["windows"].append({
            "id": slug,
            "start_s": start,
            "end_s": end,
            "duration_s": end - start,
            "audio": str(audio_out),
            "reference_json": str(reference_json),
            "reference_srt": str(reference_srt),
            "speaker_count": clipped["speaker_count"],
            "segment_count": clipped["segment_count"],
            "word_count": clipped["computed_word_count"],
        })

    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Prépare des fenêtres audio + référence depuis une longue réunion.")
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--reference-json", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--window", type=parse_window, action="append", required=True, help="Fenêtre HH:MM:SS-HH:MM:SS, répétable")
    args = parser.parse_args()

    if not args.audio.is_file():
        raise SystemExit(f"Audio introuvable: {args.audio}")
    if not args.reference_json.is_file():
        raise SystemExit(f"Référence introuvable: {args.reference_json}")

    manifest = prepare_windows(args.audio, args.reference_json, args.output_dir, args.window)
    print(f"windows={len(manifest['windows'])} output={args.output_dir}")
    for window in manifest["windows"]:
        print(
            f"{window['id']} speakers={window['speaker_count']} "
            f"segments={window['segment_count']} words={window['word_count']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

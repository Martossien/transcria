#!/usr/bin/env python3
"""Extrait une transcription DOCX horodatée en JSON/SRT de référence.

Format attendu, observé sur les exports de transcription marché :

    INTERVENANT_01
    00:02:00 - 00:02:28 DUREE : 00:00:28
    Nombre de mots : 2
    bonjour, bonjour

Le script reste volontairement strict : les blocs incomplets sont signalés dans
`parse_warnings` au lieu d'être interprétés au jugé.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from docx import Document

SPEAKER_RE = re.compile(r"^INTERVENANT[_\s-]*(\d+)$", re.IGNORECASE)
TIMING_RE = re.compile(
    r"^(?P<start>\d{1,2}:\d{2}:\d{2})\s*-\s*(?P<end>\d{1,2}:\d{2}:\d{2})"
    r"(?:\s+DUREE\s*:\s*(?P<duration>\d{1,2}:\d{2}:\d{2}))?$",
    re.IGNORECASE,
)
WORD_COUNT_RE = re.compile(r"^Nombre de mots\s*:\s*(?P<count>\d+)$", re.IGNORECASE)


@dataclass
class ReferenceSegment:
    index: int
    speaker: str
    start: float
    end: float
    duration: float
    declared_words: int | None
    word_count: int
    text: str


def hms_to_seconds(value: str) -> float:
    parts = value.strip().split(":")
    if len(parts) != 3:
        raise ValueError(f"Timestamp invalide: {value!r}")
    hours, minutes, seconds = (int(part) for part in parts)
    return float(hours * 3600 + minutes * 60 + seconds)


def seconds_to_srt_time(seconds: float) -> str:
    millis = int(round(max(seconds, 0.0) * 1000))
    hours, rem = divmod(millis, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", text, flags=re.UNICODE))


def _paragraphs(docx_path: Path) -> list[str]:
    doc = Document(docx_path)
    return [paragraph.text.strip() for paragraph in doc.paragraphs if paragraph.text.strip()]


def _speaker_name(match: re.Match[str]) -> str:
    return f"INTERVENANT_{int(match.group(1)):02d}"


def parse_reference_docx(docx_path: Path) -> dict:
    paragraphs = _paragraphs(docx_path)
    segments: list[ReferenceSegment] = []
    warnings: list[str] = []
    i = 0

    while i < len(paragraphs):
        speaker_match = SPEAKER_RE.match(paragraphs[i])
        if not speaker_match:
            warnings.append(f"paragraphe_{i}_hors_bloc: {paragraphs[i][:80]}")
            i += 1
            continue

        speaker = _speaker_name(speaker_match)
        block_start = i
        i += 1
        if i >= len(paragraphs):
            warnings.append(f"bloc_incomplet_{block_start}: timestamp_absent")
            break

        timing_match = TIMING_RE.match(paragraphs[i])
        if not timing_match:
            warnings.append(f"bloc_incomplet_{block_start}: timestamp_invalide={paragraphs[i][:80]}")
            continue
        start = hms_to_seconds(timing_match.group("start"))
        end = hms_to_seconds(timing_match.group("end"))
        duration = hms_to_seconds(timing_match.group("duration")) if timing_match.group("duration") else max(end - start, 0.0)
        i += 1

        declared_words: int | None = None
        if i < len(paragraphs):
            wc_match = WORD_COUNT_RE.match(paragraphs[i])
            if wc_match:
                declared_words = int(wc_match.group("count"))
                i += 1
            else:
                warnings.append(f"bloc_{block_start}: nombre_mots_absent")

        text_parts: list[str] = []
        while i < len(paragraphs) and not SPEAKER_RE.match(paragraphs[i]):
            text_parts.append(paragraphs[i])
            i += 1
        text = " ".join(part for part in text_parts if part).strip()
        if not text:
            warnings.append(f"bloc_{block_start}: texte_absent")

        segments.append(
            ReferenceSegment(
                index=len(segments) + 1,
                speaker=speaker,
                start=start,
                end=end,
                duration=duration,
                declared_words=declared_words,
                word_count=_word_count(text),
                text=text,
            )
        )

    speakers = sorted({segment.speaker for segment in segments})
    return {
        "schema_version": 1,
        "source_docx": str(docx_path),
        "segment_count": len(segments),
        "speaker_count": len(speakers),
        "speakers": speakers,
        "start_s": min((segment.start for segment in segments), default=None),
        "end_s": max((segment.end for segment in segments), default=None),
        "duration_s": max((segment.end for segment in segments), default=0.0) - min((segment.start for segment in segments), default=0.0)
        if segments
        else 0.0,
        "declared_word_count": sum(segment.declared_words or 0 for segment in segments),
        "computed_word_count": sum(segment.word_count for segment in segments),
        "parse_warnings": warnings,
        "segments": [asdict(segment) for segment in segments],
    }


def write_srt(reference: dict, output_path: Path) -> None:
    blocks: list[str] = []
    for idx, segment in enumerate(reference.get("segments") or [], start=1):
        text = segment.get("text") or ""
        speaker = segment.get("speaker") or "INTERVENANT"
        blocks.append(
            "\n".join(
                [
                    str(idx),
                    f"{seconds_to_srt_time(float(segment['start']))} --> {seconds_to_srt_time(float(segment['end']))}",
                    f"{speaker}: {text}",
                ]
            )
        )
    output_path.write_text("\n\n".join(blocks) + ("\n" if blocks else ""), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Extrait une transcription DOCX horodatée en JSON/SRT de référence.")
    parser.add_argument("docx", type=Path, help="Fichier DOCX source")
    parser.add_argument("--output-json", type=Path, required=True, help="JSON de référence à écrire")
    parser.add_argument("--output-srt", type=Path, default=None, help="SRT de référence optionnel")
    args = parser.parse_args()

    if not args.docx.is_file():
        raise SystemExit(f"DOCX introuvable: {args.docx}")

    reference = parse_reference_docx(args.docx)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(reference, indent=2, ensure_ascii=False), encoding="utf-8")
    if args.output_srt:
        args.output_srt.parent.mkdir(parents=True, exist_ok=True)
        write_srt(reference, args.output_srt)

    print(
        f"segments={reference['segment_count']} speakers={reference['speaker_count']} "
        f"words={reference['computed_word_count']} warnings={len(reference['parse_warnings'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

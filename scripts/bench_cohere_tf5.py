#!/usr/bin/env python3
"""Bench expérimental Cohere ASR natif Transformers 5 sur chunks pyannote.

Ce script est volontairement séparé du pipeline produit : il charge une pile
Transformers 5 isolée via --tf5-site/PYTHONPATH et réutilise des résultats E2E
existants pour récupérer audio_path + speaker_turns.json.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_MODEL_ID = "CohereLabs/cohere-transcribe-03-2026"


@dataclass(frozen=True)
class Chunk:
    start: float
    end: float
    speaker: str
    audio: Any


def seconds_to_srt_time(seconds: float) -> str:
    millis = int(round(seconds * 1000))
    hours, rem = divmod(millis, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def segments_to_srt(segments: list[dict]) -> str:
    lines = []
    for index, segment in enumerate(segments, start=1):
        speaker = segment.get("speaker") or "SPEAKER_00"
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        lines.extend([
            str(index),
            f"{seconds_to_srt_time(float(segment['start']))} --> {seconds_to_srt_time(float(segment['end']))}",
            f"{speaker}: {text}",
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"


def chunk_turns(turns: list[dict], audio, sample_rate: int, max_chunk_s: float) -> list[Chunk]:
    chunks: list[Chunk] = []
    min_samples = int(0.5 * sample_rate)
    for turn in turns:
        start = max(0.0, float(turn.get("start", 0.0)))
        end = max(start, float(turn.get("end", start)))
        speaker = str(turn.get("speaker") or "SPEAKER_00")
        pos = start
        while pos < end:
            chunk_end = min(pos + max_chunk_s, end)
            start_sample = int(pos * sample_rate)
            end_sample = int(chunk_end * sample_rate)
            chunk_audio = audio[start_sample:end_sample]
            if len(chunk_audio) >= min_samples:
                chunks.append(Chunk(start=pos, end=chunk_end, speaker=speaker, audio=chunk_audio))
            pos = chunk_end
    return chunks


def load_e2e_result(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not data.get("audio_path"):
        raise ValueError(f"{path}: champ audio_path absent")
    if not data.get("job_dir"):
        raise ValueError(f"{path}: champ job_dir absent")
    return data


def load_turns(job_dir: Path) -> list[dict]:
    turns_path = job_dir / "speakers" / "speaker_turns.json"
    data = json.loads(turns_path.read_text(encoding="utf-8"))
    turns = data.get("exclusive_turns") or []
    if not turns:
        raise ValueError(f"{turns_path}: aucun exclusive_turns")
    return turns


def output_dir_for_result(output_root: Path, result_path: Path, result: dict) -> Path:
    audio_path = Path(str(result["audio_path"]))
    window_name = audio_path.stem
    if result_path.parent.name:
        window_name = result_path.parent.name
    return output_root / window_name


def decode_generated(processor, generated, inputs: dict) -> list[str]:
    pad_token_id = processor.tokenizer.pad_token_id
    prompt_lens = inputs["decoder_input_ids"].ne(pad_token_id).sum(dim=1).detach().cpu().tolist()
    texts: list[str] = []
    eos_token_id = processor.tokenizer.eos_token_id
    for row, prompt_len in enumerate(prompt_lens):
        token_ids = generated[row].detach().cpu().tolist()
        prompt_ids = inputs["decoder_input_ids"][row, :prompt_len].detach().cpu().tolist()
        if len(token_ids) >= prompt_len and token_ids[:prompt_len] == prompt_ids:
            token_ids = token_ids[prompt_len:]
        if eos_token_id in token_ids:
            token_ids = token_ids[:token_ids.index(eos_token_id)]
        texts.append(processor.decode(token_ids, skip_special_tokens=True).strip())
    return texts


def transcribe_chunks(
    chunks: list[Chunk],
    processor,
    model,
    torch,
    device: str,
    *,
    language: str,
    punctuation: bool,
    batch_size: int,
    max_new_tokens: int,
    repetition_penalty: float,
    no_repeat_ngram_size: int,
) -> tuple[list[dict], float]:
    segments: list[dict] = []
    generate_s = 0.0
    for offset in range(0, len(chunks), batch_size):
        batch = chunks[offset:offset + batch_size]
        inputs = processor(
            audio=[chunk.audio for chunk in batch],
            language=language,
            punctuation=punctuation,
            sampling_rate=16000,
            return_tensors="pt",
        )
        for key, value in list(inputs.items()):
            if hasattr(value, "to"):
                inputs[key] = value.to(device)
        inputs["input_features"] = inputs["input_features"].to(torch.bfloat16)
        start = time.time()
        with torch.inference_mode():
            generated = model.generate(
                input_features=inputs["input_features"],
                attention_mask=inputs.get("attention_mask"),
                decoder_input_ids=inputs["decoder_input_ids"],
                decoder_start_token_id=int(inputs["decoder_input_ids"][0, 0].item()),
                max_new_tokens=max_new_tokens,
                do_sample=False,
                no_repeat_ngram_size=no_repeat_ngram_size,
                repetition_penalty=repetition_penalty,
            )
        generate_s += time.time() - start
        texts = decode_generated(processor, generated, inputs)
        for chunk, text in zip(batch, texts):
            if text:
                segments.append({
                    "start": round(chunk.start, 3),
                    "end": round(chunk.end, 3),
                    "speaker": chunk.speaker,
                    "text": text,
                })
    return segments, generate_s


def run_one(result_path: Path, args: argparse.Namespace, processor, model, torch, librosa) -> dict:
    result = load_e2e_result(result_path)
    audio_path = Path(str(result["audio_path"]))
    job_dir = Path(str(result["job_dir"]))
    turns = load_turns(job_dir)

    audio, sample_rate = librosa.load(str(audio_path), sr=16000, mono=True)
    chunks = chunk_turns(turns, audio, sample_rate, args.max_chunk_s)

    start = time.time()
    segments, generate_s = transcribe_chunks(
        chunks,
        processor,
        model,
        torch,
        args.device,
        language=args.language,
        punctuation=args.punctuation,
        batch_size=args.batch_size,
        max_new_tokens=args.max_new_tokens,
        repetition_penalty=args.repetition_penalty,
        no_repeat_ngram_size=args.no_repeat_ngram_size,
    )
    elapsed_s = time.time() - start
    srt = segments_to_srt(segments)
    text = " ".join(segment["text"] for segment in segments)

    out_dir = output_dir_for_result(args.output_dir, result_path, result)
    out_dir.mkdir(parents=True, exist_ok=True)
    output = {
        "combo_id": args.combo_id,
        "status": "ok",
        "effective_stt_backend": "cohere_tf5_pyannote_chunks",
        "stt_backend": "cohere",
        "source_result": str(result_path),
        "audio_path": str(audio_path),
        "job_dir": str(job_dir),
        "srt": {
            "raw_content": srt,
            "raw_words": len(text.split()),
            "raw_segments": len(segments),
        },
        "transcription_segments": segments,
        "_elapsed_wall_s": round(elapsed_s, 3),
        "native_tf5": {
            "chunks": len(chunks),
            "segments": len(segments),
            "generate_s": round(generate_s, 3),
            "language": args.language,
            "punctuation": args.punctuation,
            "batch_size": args.batch_size,
            "max_chunk_s": args.max_chunk_s,
            "max_new_tokens": args.max_new_tokens,
            "repetition_penalty": args.repetition_penalty,
            "no_repeat_ngram_size": args.no_repeat_ngram_size,
            "model_id": args.model_id,
        },
    }
    (out_dir / f"{args.combo_id}.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / f"{args.combo_id}.srt").write_text(srt, encoding="utf-8")
    (out_dir / f"{args.combo_id}.txt").write_text(text, encoding="utf-8")
    return output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bench Cohere Transformers 5 isolé sur chunks pyannote existants.")
    parser.add_argument("--result-json", type=Path, nargs="+", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--tf5-site", type=Path, default=Path("/tmp/transcria_tf54_site"))
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--language", default="fr")
    parser.add_argument("--punctuation", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--batch-size", type=int, default=96)
    parser.add_argument("--max-chunk-s", type=float, default=30.0)
    parser.add_argument("--max-new-tokens", type=int, default=448)
    parser.add_argument("--repetition-penalty", type=float, default=1.2)
    parser.add_argument("--no-repeat-ngram-size", type=int, default=4)
    parser.add_argument("--combo-id", default="T11")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.tf5_site:
        sys.path.insert(0, str(args.tf5_site))

    import librosa
    import torch
    from transformers import AutoProcessor, CohereAsrForConditionalGeneration

    args.output_dir.mkdir(parents=True, exist_ok=True)
    load_start = time.time()
    processor = AutoProcessor.from_pretrained(args.model_id, local_files_only=True)
    model = CohereAsrForConditionalGeneration.from_pretrained(
        args.model_id,
        local_files_only=True,
        dtype=torch.bfloat16,
    ).to(args.device).eval()
    load_s = time.time() - load_start

    rows = []
    for result_path in args.result_json:
        started = time.time()
        result = run_one(result_path, args, processor, model, torch, librosa)
        rows.append({
            "source_result": str(result_path),
            "output": str(output_dir_for_result(args.output_dir, result_path, result) / f"{args.combo_id}.json"),
            "chunks": result["native_tf5"]["chunks"],
            "segments": result["native_tf5"]["segments"],
            "words": result["srt"]["raw_words"],
            "elapsed_s": result["_elapsed_wall_s"],
            "wall_s": round(time.time() - started, 3),
        })
        print(rows[-1], flush=True)

    manifest = {
        "model_id": args.model_id,
        "device": args.device,
        "tf5_site": str(args.tf5_site),
        "load_s": round(load_s, 3),
        "result_count": len(rows),
        "results": rows,
    }
    (args.output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"done output_dir={args.output_dir} results={len(rows)} load_s={load_s:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Worker subprocess pour Cohere TF5.

Ce module est lancé dans un process frais avec PYTHONPATH pointant d'abord vers
le site-packages Transformers 5 isolé. Il ne doit pas importer l'application
Flask ni pyannote.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np


def _decode_generated(processor, generated, inputs: dict) -> list[str]:
    tokenizer = processor.tokenizer
    pad_token_id = tokenizer.pad_token_id
    eos_token_id = tokenizer.eos_token_id
    prompt_lens = inputs["decoder_input_ids"].ne(pad_token_id).sum(dim=1).detach().cpu().tolist()
    texts: list[str] = []
    for row, prompt_len in enumerate(prompt_lens):
        token_ids = generated[row].detach().cpu().tolist()
        prompt_ids = inputs["decoder_input_ids"][row, :prompt_len].detach().cpu().tolist()
        if len(token_ids) >= prompt_len and token_ids[:prompt_len] == prompt_ids:
            token_ids = token_ids[prompt_len:]
        if eos_token_id in token_ids:
            token_ids = token_ids[:token_ids.index(eos_token_id)]
        texts.append(processor.decode(token_ids, skip_special_tokens=True).strip())
    return texts


def _generate_batch(processor, model, torch, device: str, audio_batch: list, cfg: dict) -> list[str]:
    inputs = processor(
        audio=audio_batch,
        language=cfg["language"],
        punctuation=cfg["punctuation"],
        sampling_rate=16000,
        return_tensors="pt",
    )
    for key, value in list(inputs.items()):
        if hasattr(value, "to"):
            inputs[key] = value.to(device)
    inputs["input_features"] = inputs["input_features"].to(torch.bfloat16)
    with torch.inference_mode():
        generated = model.generate(
            input_features=inputs["input_features"],
            attention_mask=inputs.get("attention_mask"),
            decoder_input_ids=inputs["decoder_input_ids"],
            decoder_start_token_id=int(inputs["decoder_input_ids"][0, 0].item()),
            max_new_tokens=cfg["max_new_tokens"],
            do_sample=False,
            no_repeat_ngram_size=cfg["no_repeat_ngram_size"],
            repetition_penalty=cfg["repetition_penalty"],
        )
    return _decode_generated(processor, generated, inputs)


def run(input_path: Path, output_path: Path) -> int:
    import torch
    import transformers
    from transformers import AutoProcessor

    CohereAsrForConditionalGeneration = getattr(transformers, "CohereAsrForConditionalGeneration")

    request = json.loads(input_path.read_text(encoding="utf-8"))
    arrays = np.load(request["arrays_path"])
    chunks = request["chunks"]
    cfg = request["config"]
    device = cfg["device"]

    processor_kwargs = {"local_files_only": True}
    model_kwargs = {"local_files_only": True, "dtype": torch.bfloat16}
    if cfg.get("model_revision"):
        processor_kwargs["revision"] = cfg["model_revision"]
        model_kwargs["revision"] = cfg["model_revision"]

    processor = AutoProcessor.from_pretrained(cfg["model_path"], **processor_kwargs)
    model = CohereAsrForConditionalGeneration.from_pretrained(cfg["model_path"], **model_kwargs).to(device).eval()

    segments = []
    batch_size = max(1, int(cfg["batch_size"]))
    for offset in range(0, len(chunks), batch_size):
        batch_meta = chunks[offset:offset + batch_size]
        audio_batch = [arrays[meta["array_key"]] for meta in batch_meta]
        texts = _generate_batch(processor, model, torch, device, audio_batch, cfg)
        for meta, text in zip(batch_meta, texts):
            segments.append({
                "start": meta["start"],
                "end": meta["end"],
                "speaker": meta.get("speaker"),
                "text": text,
            })

    output_path.write_text(json.dumps({"segments": segments}, ensure_ascii=False), encoding="utf-8")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    return run(args.input, args.output)


if __name__ == "__main__":
    raise SystemExit(main())

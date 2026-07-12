"""Worker subprocess pour MOSS-Transcribe-Diarize (Transformers 5).

Lancé dans un process frais avec PYTHONPATH pointant d'abord vers le
site-packages isolé (``moss.moss_site``) qui contient Transformers 5.x et le
paquet ``moss_transcribe_diarize`` — le venv projet reste en Transformers 4.x.
Il ne doit importer ni l'application Flask ni pyannote. Même patron que
``_cohere_tf5_worker``.

Entrée (JSON), deux modes :
- fichier entier : {"audio_path": ..., "config": {...}} ;
- tours pré-découpés (pipeline pyannote) : {"arrays_path": <npz>, "chunks":
  [{array_key, start, end, speaker}], "config": {...}} — le modèle n'est
  chargé qu'UNE fois pour tous les tours (un subprocess par tour serait
  inutilisable : ~15 s de chargement × N tours).
Sortie (JSON) : {"segments": [{start, end, speaker, text}], "raw_text": ...}.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def run(input_path: Path, output_path: Path) -> int:
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    cfg = payload["config"]

    import torch
    from moss_transcribe_diarize import parse_transcript
    from moss_transcribe_diarize.inference_utils import (
        build_transcription_messages,
        generate_transcription,
    )
    from transformers import AutoModelForCausalLM, AutoProcessor

    device = torch.device(cfg.get("device") or ("cuda:0" if torch.cuda.is_available() else "cpu"))
    dtype = torch.bfloat16 if device.type == "cuda" else torch.float32
    # mypy (stubs transformers 4.x du venv) se méprend sur from_pretrained ici :
    # ce module ne s'exécute QUE sous le site transformers 5 isolé.
    model = AutoModelForCausalLM.from_pretrained(  # type: ignore[call-arg]
        cfg["model_path"], trust_remote_code=True, dtype="auto"
    ).to(dtype=dtype).to(device).eval()
    processor = AutoProcessor.from_pretrained(cfg["model_path"], trust_remote_code=True)

    max_new_tokens = int(cfg.get("max_new_tokens") or 8192)

    def transcribe_path(path: str) -> tuple[list, str]:
        result = generate_transcription(
            model,
            processor,
            build_transcription_messages(path),
            max_new_tokens=max_new_tokens,
            do_sample=False,
            device=device,
            dtype=dtype,
        )
        raw = result["text"]
        return list(parse_transcript(raw)), raw

    segments: list[dict] = []
    raw_texts: list[str] = []
    if payload.get("chunks"):
        # Mode tours pré-découpés : timestamps recalés en absolu, locuteur du
        # tour pyannote conservé (les étiquettes une-passe n'ont pas de sens
        # sur un tour mono-locuteur de quelques secondes).
        import tempfile

        import numpy as np
        import soundfile as sf

        arrays = np.load(payload["arrays_path"])
        with tempfile.TemporaryDirectory(prefix="moss-chunks-") as tmp:
            for index, chunk in enumerate(payload["chunks"]):
                wav = Path(tmp) / f"{index}.wav"
                sf.write(str(wav), arrays[chunk["array_key"]], 16000)
                parsed, raw = transcribe_path(str(wav))
                raw_texts.append(raw)
                offset = float(chunk["start"])
                for seg in parsed:
                    if not seg.text or not seg.text.strip():
                        continue
                    segments.append({
                        "start": round(offset + float(seg.start), 3),
                        "end": round(offset + float(seg.end), 3),
                        "speaker": chunk.get("speaker"),
                        "text": seg.text.strip(),
                    })
    else:
        parsed, raw = transcribe_path(payload["audio_path"])
        raw_texts.append(raw)
        segments = [
            {
                "start": round(float(seg.start), 3),
                "end": round(float(seg.end), 3),
                "speaker": seg.speaker,
                "text": seg.text.strip(),
            }
            for seg in parsed
            if seg.text and seg.text.strip()
        ]
    output_path.write_text(
        json.dumps({"segments": segments, "raw_text": "\n".join(raw_texts)}, ensure_ascii=False),
        encoding="utf-8",
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    return run(Path(args.input), Path(args.output))


if __name__ == "__main__":
    raise SystemExit(main())

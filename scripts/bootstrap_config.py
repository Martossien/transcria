#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import os
from pathlib import Path

import yaml

from transcria.config import _deep_merge, validate_config
from transcria.config.system_detector import SystemDetector


def _load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _detect_defaults(base_dir: Path) -> dict:
    info = SystemDetector.detect()
    config = {
        "storage": {
            "jobs_dir": str((base_dir / "jobs").resolve()),
            "database_url": f"sqlite:///{(base_dir / 'transcrIA.db').resolve()}",
        },
        "services": {},
        "models": {},
    }

    binaries = {b.name: b for b in info.binaries}
    opencode_bin = binaries.get("opencode")
    ffmpeg_bin = binaries.get("ffmpeg")
    ffprobe_bin = binaries.get("ffprobe")

    if opencode_bin and opencode_bin.path:
        config["workflow"] = {
            "arbitration_llm": {
                "opencode_bin": opencode_bin.path,
            }
        }

    config["services"]["ffmpeg_available"] = bool(ffmpeg_bin and ffmpeg_bin.path)
    config["services"]["ffprobe_available"] = bool(ffprobe_bin and ffprobe_bin.path)

    cohere_path = base_dir / "models" / "cohere-asr" / "cohere-transcribe-03-2026"
    if cohere_path.exists():
        config["models"]["cohere_model_path"] = str(cohere_path.resolve())

    return config


def bootstrap_config(example_path: Path, output_path: Path, force: bool = False) -> tuple[dict, list[str]]:
    if output_path.exists() and not force:
        raise FileExistsError(f"Le fichier existe déjà: {output_path}")

    template = _load_yaml(example_path)
    detected = _detect_defaults(output_path.parent)
    merged = _deep_merge(copy.deepcopy(template), detected)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(merged, fh, allow_unicode=True, sort_keys=False)

    result = validate_config(merged)
    return merged, result.all_messages


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap semi-automatique de config.yaml pour TranscrIA")
    parser.add_argument("--example", default="config.example.yaml", help="Chemin du template YAML")
    parser.add_argument("--output", default="config.yaml", help="Chemin du fichier de sortie")
    parser.add_argument("--force", action="store_true", help="Écraser le fichier de sortie s'il existe")
    args = parser.parse_args()

    example_path = Path(args.example).resolve()
    output_path = Path(args.output).resolve()

    merged, messages = bootstrap_config(example_path, output_path, force=args.force)

    print(f"Configuration générée: {output_path}")
    print(f"Jobs dir: {merged.get('storage', {}).get('jobs_dir')}")
    print(f"Database URL: {merged.get('storage', {}).get('database_url')}")
    if messages:
        print("Validation / avertissements:")
        for msg in messages:
            print(f"- {msg}")
    else:
        print("Validation: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

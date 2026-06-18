from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path
from typing import Any

PYANNOTE_MODEL_ID = "pyannote/speaker-diarization-community-1"


def resolve_repo_relative_path(path: str, install_dir: Path) -> Path:
    """Résout les chemins `./...` de config.yaml depuis la racine d'installation."""
    if path.startswith("./"):
        return Path(install_dir) / path[2:]
    return Path(path)


def is_non_empty_dir(path: Path) -> bool:
    """Retourne vrai si `path` est un répertoire non vide."""
    path = Path(path)
    return path.is_dir() and any(path.iterdir())


def find_pyannote_cache(hf_cache: Path) -> Path | None:
    """Retourne le premier cache pyannote speaker-diarization trouvé."""
    hf_cache = Path(hf_cache)
    matches = sorted(hf_cache.glob("models--pyannote--speaker-diarization*"))
    for match in matches:
        if match.is_dir():
            return match
    return None


def find_first_gguf(models_dir: Path) -> Path | None:
    """Retourne le premier fichier GGUF trouvé dans l'arborescence des modèles."""
    models_dir = Path(models_dir)
    matches = sorted(models_dir.rglob("*.gguf")) if models_dir.exists() else []
    for match in matches:
        if match.is_file():
            return match
    return None


def download_pyannote_pipeline(hf_token: str, *, model_id: str = PYANNOTE_MODEL_ID, pipeline_cls: Any | None = None) -> None:
    """Télécharge le pipeline pyannote en cache HuggingFace."""
    if not hf_token:
        raise ValueError("HF_TOKEN requis")
    if pipeline_cls is None:
        module = importlib.import_module("pyannote.audio")
        pipeline_cls = module.Pipeline
    pipeline_cls.from_pretrained(model_id, use_auth_token=hf_token)


def _print_path(path: Path | None) -> int:
    if path is None:
        return 1
    print(path)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Détection locale des modèles TranscrIA pour install.sh.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    cohere_parser = subparsers.add_parser("cohere-ok", help="teste si le dossier Cohere existe et est non vide")
    cohere_parser.add_argument("--path", required=True)
    cohere_parser.add_argument("--install-dir", required=True)

    pyannote_parser = subparsers.add_parser("pyannote-cache", help="trouve le cache pyannote local")
    pyannote_parser.add_argument("--hf-cache", required=True)

    gguf_parser = subparsers.add_parser("first-gguf", help="trouve le premier modèle GGUF local")
    gguf_parser.add_argument("--models-dir", required=True)

    pyannote_download_parser = subparsers.add_parser("download-pyannote", help="précharge pyannote dans le cache HuggingFace")
    pyannote_download_parser.add_argument("--hf-token", required=True)
    pyannote_download_parser.add_argument("--model-id", default=PYANNOTE_MODEL_ID)

    args = parser.parse_args(argv)
    try:
        if args.command == "cohere-ok":
            path = resolve_repo_relative_path(args.path, Path(args.install_dir))
            return 0 if is_non_empty_dir(path) else 1
        if args.command == "pyannote-cache":
            return _print_path(find_pyannote_cache(Path(args.hf_cache)))
        if args.command == "first-gguf":
            return _print_path(find_first_gguf(Path(args.models_dir)))
        if args.command == "download-pyannote":
            download_pyannote_pipeline(args.hf_token, model_id=args.model_id)
            print("pyannote téléchargé")
            return 0
    except (OSError, ImportError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"commande inconnue: {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path
from typing import Any

PYANNOTE_MODEL_ID = "pyannote/speaker-diarization-community-1"


def parse_bool(value: str) -> bool:
    """Parse un booléen CLI stable pour les appels depuis install.sh."""
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"booléen invalide: {value}")


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


def render_model_summary(
    *,
    profile: str,
    needs_local_models: bool,
    needs_llm: bool,
    cohere_ok: bool,
    pyannote_ok: bool,
    qwen_ok: bool,
    opencode_bin: str,
) -> str:
    """Rend le bilan final des modèles à partir des états déjà détectés."""
    lines = ["Modèles IA :"]
    if needs_local_models:
        lines.append(
            "  [OK] Cohere ASR"
            if cohere_ok
            else "  [MANQUANT] Cohere ASR — huggingface-cli download CohereLabs/cohere-transcribe-03-2026"
        )
        lines.append(
            "  [OK] pyannote diarization"
            if pyannote_ok
            else "  [MANQUANT] pyannote — HF_TOKEN dans .env + accepter conditions HuggingFace"
        )
    else:
        lines.append(f"  [INFO] Modèles GPU locaux non requis pour le profil {profile}")

    if needs_llm:
        lines.append("  [OK] LLM d'arbitrage GGUF" if qwen_ok else "  [MANQUANT] LLM d'arbitrage GGUF — choisir un palier dans install.sh")
        if opencode_bin:
            lines.append(f"  [OK] opencode : {opencode_bin}")
        else:
            lines.append("  [MANQUANT] opencode — résumé/correction LLM désactivé")
    else:
        lines.append(f"  [INFO] LLM/opencode non requis pour le profil {profile}")
    return "\n".join(lines) + "\n"


def _basename_or_empty(path: str) -> str:
    return Path(path).name if path else ""


def render_model_detection_table(
    *,
    cohere_ok: bool,
    cohere_path: str,
    pyannote_ok: bool,
    pyannote_cache: str,
    needs_llm: bool,
    qwen_ok: bool,
    qwen_gguf: str,
    squim_ok: bool,
) -> str:
    """Rend le tableau de vérification des modèles locaux."""
    rows = [
        (
            "Cohere ASR (STT ~6 Go)",
            "OK" if cohere_ok else "MANQUANT",
            _basename_or_empty(cohere_path) if cohere_ok else "huggingface-cli download CohereLabs/...",
        ),
        (
            "pyannote diarization (~2 Go)",
            "OK" if pyannote_ok else "MANQUANT",
            _basename_or_empty(pyannote_cache) if pyannote_ok else "HF_TOKEN requis + accepter conditions HF",
        ),
    ]
    if needs_llm:
        rows.append(
            (
                "LLM arbitrage GGUF",
                "OK" if qwen_ok else "MANQUANT",
                _basename_or_empty(qwen_gguf) if qwen_ok else "palier configurable via install.sh",
            )
        )
    rows.append(
        (
            "SQUIM préflight (~28 Mo)",
            "OK" if squim_ok else "MANQUANT",
            "cache torchaudio" if squim_ok else "cf. docs/INSTALL.md § Réseau d'entreprise",
        )
    )

    lines = ["Modèles détectés :"]
    lines.extend(f"  - {name}: {status} — {info}" for name, status, info in rows)
    return "\n".join(lines) + "\n"


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

    summary_parser = subparsers.add_parser("summary", help="rend le bilan final des modèles")
    summary_parser.add_argument("--profile", required=True)
    summary_parser.add_argument("--needs-local-models", required=True)
    summary_parser.add_argument("--needs-llm", required=True)
    summary_parser.add_argument("--cohere-ok", required=True)
    summary_parser.add_argument("--pyannote-ok", required=True)
    summary_parser.add_argument("--qwen-ok", required=True)
    summary_parser.add_argument("--opencode-bin", default="")

    table_parser = subparsers.add_parser("detection-table", help="rend le tableau de vérification des modèles locaux")
    table_parser.add_argument("--cohere-ok", required=True)
    table_parser.add_argument("--cohere-path", default="")
    table_parser.add_argument("--pyannote-ok", required=True)
    table_parser.add_argument("--pyannote-cache", default="")
    table_parser.add_argument("--needs-llm", required=True)
    table_parser.add_argument("--qwen-ok", required=True)
    table_parser.add_argument("--qwen-gguf", default="")
    table_parser.add_argument("--squim-ok", required=True)

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
        if args.command == "summary":
            print(
                render_model_summary(
                    profile=args.profile,
                    needs_local_models=parse_bool(args.needs_local_models),
                    needs_llm=parse_bool(args.needs_llm),
                    cohere_ok=parse_bool(args.cohere_ok),
                    pyannote_ok=parse_bool(args.pyannote_ok),
                    qwen_ok=parse_bool(args.qwen_ok),
                    opencode_bin=args.opencode_bin,
                ),
                end="",
            )
            return 0
        if args.command == "detection-table":
            print(
                render_model_detection_table(
                    cohere_ok=parse_bool(args.cohere_ok),
                    cohere_path=args.cohere_path,
                    pyannote_ok=parse_bool(args.pyannote_ok),
                    pyannote_cache=args.pyannote_cache,
                    needs_llm=parse_bool(args.needs_llm),
                    qwen_ok=parse_bool(args.qwen_ok),
                    qwen_gguf=args.qwen_gguf,
                    squim_ok=parse_bool(args.squim_ok),
                ),
                end="",
            )
            return 0
    except (OSError, ImportError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"commande inconnue: {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

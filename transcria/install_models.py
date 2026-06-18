from __future__ import annotations

import argparse
import importlib
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from transcria.install_prerequisites import first_available

PYANNOTE_MODEL_ID = "pyannote/speaker-diarization-community-1"
SQUIM_RELATIVE_PATH = Path("hub") / "torchaudio" / "models" / "squim_objective_dns2020.pth"
COHERE_MODEL_ID = "CohereLabs/cohere-transcribe-03-2026"
COHERE_DEFAULT_RELATIVE_PATH = Path("models") / "cohere-asr" / "cohere-transcribe-03-2026"


@dataclass(frozen=True)
class LocalModelDetection:
    cohere_path: Path
    cohere_ok: bool
    pyannote_cache: Path | None
    pyannote_ok: bool
    squim_path: Path
    squim_ok: bool
    qwen_gguf: Path | None
    qwen_ok: bool


@dataclass(frozen=True)
class CohereDownloadPlan:
    destination: Path
    cli_name: str
    cli_path: Path | None


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


def detect_local_models(
    *,
    cohere_path: str,
    install_dir: Path,
    hf_cache: Path,
    torch_home: Path,
    models_dir: Path,
    needs_llm: bool,
) -> LocalModelDetection:
    """Détecte les modèles locaux utilisés par l'installateur."""
    resolved_cohere_path = resolve_repo_relative_path(cohere_path, install_dir)
    pyannote_cache = find_pyannote_cache(hf_cache)
    squim_path = torch_home / SQUIM_RELATIVE_PATH
    qwen_gguf = find_first_gguf(models_dir) if needs_llm else None
    return LocalModelDetection(
        cohere_path=resolved_cohere_path,
        cohere_ok=bool(cohere_path) and is_non_empty_dir(resolved_cohere_path),
        pyannote_cache=pyannote_cache,
        pyannote_ok=pyannote_cache is not None,
        squim_path=squim_path,
        squim_ok=squim_path.is_file(),
        qwen_gguf=qwen_gguf,
        qwen_ok=qwen_gguf is not None,
    )


def render_local_model_detection_shell(detection: LocalModelDetection) -> str:
    """Rend la détection locale sous forme d'affectations shell filtrables."""
    values = {
        "COHERE_PATH": str(detection.cohere_path),
        "COHERE_OK": str(detection.cohere_ok).lower(),
        "PYANNOTE_CACHE": str(detection.pyannote_cache or ""),
        "PYANNOTE_OK": str(detection.pyannote_ok).lower(),
        "SQUIM_PTH": str(detection.squim_path),
        "SQUIM_OK": str(detection.squim_ok).lower(),
        "QWEN_GGUF": str(detection.qwen_gguf or ""),
        "QWEN_OK": str(detection.qwen_ok).lower(),
    }
    return "".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items())


def plan_cohere_download(*, install_dir: Path) -> CohereDownloadPlan:
    """Prépare le téléchargement Cohere sans lancer d'action réseau."""
    cli = first_available(["huggingface-cli"])
    return CohereDownloadPlan(
        destination=Path(install_dir) / COHERE_DEFAULT_RELATIVE_PATH,
        cli_name=cli.name if cli else "",
        cli_path=cli.path if cli else None,
    )


def render_cohere_download_plan_shell(plan: CohereDownloadPlan) -> str:
    """Rend le plan de téléchargement Cohere sous forme d'affectations shell filtrables."""
    values = {
        "COHERE_DEST": str(plan.destination),
        "COHERE_CLI": plan.cli_name,
        "COHERE_CLI_PATH": str(plan.cli_path or ""),
        "COHERE_MODEL_ID": COHERE_MODEL_ID,
    }
    return "".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items())


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


def render_model_status_log(*, event: str, value: str = "", profile: str = "") -> str:
    """Rend une ligne de statut de vérification locale des modèles."""
    if event == "cohere-ok":
        return f"OK:Cohere ASR       : {value}\n"
    if event == "cohere-missing":
        return f"WARN:Cohere ASR       : ABSENT  ({value})\n"
    if event == "pyannote-ok":
        return f"OK:pyannote cache   : {_basename_or_empty(value)}\n"
    if event == "pyannote-missing":
        return "WARN:pyannote cache   : ABSENT  (téléchargement requis, HF_TOKEN nécessaire)\n"
    if event == "squim-ok":
        return f"OK:SQUIM préflight  : {value}\n"
    if event == "squim-missing":
        return "WARN:SQUIM préflight  : ABSENT — téléchargé au 1er job (proxy requis si réseau filtré)\n"
    if event == "llm-ok":
        return f"OK:LLM arbitrage    : {value}\n"
    if event == "llm-missing":
        return "WARN:LLM arbitrage    : ABSENT  (résumé/correction LLM non disponible)\n"
    if event == "llm-not-required":
        return f"INFO:LLM d'arbitrage : non requis pour le profil {profile}\n"
    if event == "local-models-skipped":
        return f"INFO:Profil {profile} : vérification des modèles GPU locaux sautée\n"
    raise ValueError(f"événement modèle inconnu : {event}")


def render_cohere_setup_log(*, event: str, value: str = "") -> str:
    """Rend les messages interactifs de configuration du modèle Cohere."""
    if event == "missing":
        return "WARN:Le modèle Cohere ASR est introuvable au chemin configuré.\n"
    if event == "current-path":
        return f"INFO:Chemin actuel dans config.yaml : {value}\n"
    if event == "path-updated":
        return f"OK:cohere_model_path mis à jour : {value}\n"
    if event == "path-missing":
        return "WARN:Chemin introuvable — config inchangée\n"
    if event == "download-start":
        return "INFO:Téléchargement de CohereLabs/cohere-transcribe-03-2026...\n"
    if event == "download-ok":
        return "OK:Modèle Cohere téléchargé et configuré\n"
    if event == "download-failed":
        return "ERROR:Téléchargement échoué — vérifiez vos accès HuggingFace\n"
    if event == "cli-missing":
        return "WARN:huggingface-cli non trouvé — installer avec: pip install huggingface_hub\n"
    if event == "manual-command-title":
        return "INFO:Commande manuelle :\n"
    if event == "manual-command":
        return f"INFO:  huggingface-cli download CohereLabs/cohere-transcribe-03-2026 --local-dir {value} --local-dir-use-symlinks False\n"
    if event == "ignored":
        return "INFO:Modèle Cohere ignoré — pipeline STT désactivé\n"
    raise ValueError(f"événement Cohere inconnu : {event}")


def render_cohere_setup_prompt() -> str:
    """Rend le prompt interactif de configuration Cohere."""
    return "\n".join([
        "",
        "  Options :",
        "   1. Entrer le chemin où le modèle est déjà téléchargé",
        "   2. Télécharger maintenant (nécessite huggingface-cli + accès CohereLabs)",
        "   3. Ignorer (pipeline STT non fonctionnel)",
        "",
        "  Votre choix [1/2/3] : ",
    ])


def render_pyannote_setup_log(*, event: str) -> str:
    """Rend les messages interactifs de configuration pyannote."""
    if event == "missing-token":
        return "WARN:HF_TOKEN manquant — requis pour télécharger pyannote\n"
    if event == "create-token-url":
        return "INFO:(Créer un token sur https://huggingface.co/settings/tokens)\n"
    if event == "accept-terms-url":
        return "INFO:(Accepter les conditions : https://huggingface.co/pyannote/speaker-diarization-community-1)\n"
    if event == "token-saved":
        return "OK:HF_TOKEN sauvegardé dans .env\n"
    if event == "download-start":
        return "INFO:Téléchargement pyannote (peut prendre quelques minutes)...\n"
    if event == "download-ok":
        return "OK:pyannote téléchargé\n"
    if event == "download-failed":
        return "ERROR:Téléchargement pyannote échoué — vérifiez le token et les conditions HF\n"
    raise ValueError(f"événement pyannote inconnu : {event}")


def render_pyannote_token_prompt() -> str:
    """Rend le prompt de saisie silencieuse du token HuggingFace."""
    return "  HF_TOKEN (laisser vide pour ignorer) : "


def render_pyannote_download_prompt() -> str:
    """Rend la question de préchargement pyannote."""
    return "Télécharger pyannote/speaker-diarization-community-1 maintenant ?"


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

    detect_parser = subparsers.add_parser("detect-local", help="détecte les modèles locaux et rend des variables shell")
    detect_parser.add_argument("--cohere-path", required=True)
    detect_parser.add_argument("--install-dir", required=True)
    detect_parser.add_argument("--hf-cache", required=True)
    detect_parser.add_argument("--torch-home", required=True)
    detect_parser.add_argument("--models-dir", required=True)
    detect_parser.add_argument("--needs-llm", required=True)

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

    status_parser = subparsers.add_parser("status-log", help="rend une ligne de statut modèle")
    status_parser.add_argument("--event", required=True)
    status_parser.add_argument("--value", default="")
    status_parser.add_argument("--profile", default="")

    cohere_log_parser = subparsers.add_parser("cohere-setup-log", help="rend un message de configuration Cohere")
    cohere_log_parser.add_argument("--event", required=True)
    cohere_log_parser.add_argument("--value", default="")

    subparsers.add_parser("cohere-setup-prompt", help="rend le prompt de configuration Cohere")

    cohere_download_plan_parser = subparsers.add_parser("cohere-download-plan", help="prépare le téléchargement Cohere")
    cohere_download_plan_parser.add_argument("--install-dir", required=True)

    pyannote_log_parser = subparsers.add_parser("pyannote-setup-log", help="rend un message de configuration pyannote")
    pyannote_log_parser.add_argument("--event", required=True)

    subparsers.add_parser("pyannote-token-prompt", help="rend le prompt HF_TOKEN pyannote")
    subparsers.add_parser("pyannote-download-prompt", help="rend la question de téléchargement pyannote")

    args = parser.parse_args(argv)
    try:
        if args.command == "cohere-ok":
            path = resolve_repo_relative_path(args.path, Path(args.install_dir))
            return 0 if is_non_empty_dir(path) else 1
        if args.command == "pyannote-cache":
            return _print_path(find_pyannote_cache(Path(args.hf_cache)))
        if args.command == "first-gguf":
            return _print_path(find_first_gguf(Path(args.models_dir)))
        if args.command == "detect-local":
            print(
                render_local_model_detection_shell(
                    detect_local_models(
                        cohere_path=args.cohere_path,
                        install_dir=Path(args.install_dir),
                        hf_cache=Path(args.hf_cache),
                        torch_home=Path(args.torch_home),
                        models_dir=Path(args.models_dir),
                        needs_llm=parse_bool(args.needs_llm),
                    )
                ),
                end="",
            )
            return 0
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
        if args.command == "status-log":
            print(render_model_status_log(event=args.event, value=args.value, profile=args.profile), end="")
            return 0
        if args.command == "cohere-setup-log":
            print(render_cohere_setup_log(event=args.event, value=args.value), end="")
            return 0
        if args.command == "cohere-setup-prompt":
            print(render_cohere_setup_prompt(), end="")
            return 0
        if args.command == "cohere-download-plan":
            print(render_cohere_download_plan_shell(plan_cohere_download(install_dir=Path(args.install_dir))), end="")
            return 0
        if args.command == "pyannote-setup-log":
            print(render_pyannote_setup_log(event=args.event), end="")
            return 0
        if args.command == "pyannote-token-prompt":
            print(render_pyannote_token_prompt(), end="")
            return 0
        if args.command == "pyannote-download-prompt":
            print(render_pyannote_download_prompt(), end="")
            return 0
    except (OSError, ImportError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"commande inconnue: {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

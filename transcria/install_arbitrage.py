from __future__ import annotations

import argparse
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from transcria.config.gpu_calibration import apply_gpu_calibration
from transcria.config.yaml_file import get_yaml_value, load_yaml_file, set_yaml_file_value
from transcria.install_prerequisites import first_available

TIER_VRAM_MB: dict[str, int] = {
    "12gb": 12000,
    "16gb": 16000,
    "24gb": 24000,
    "32gb": 32000,
    "48gb": 48000,
    "64gb": 60000,
}

TIER_GPU_INDICES: dict[str, list[int]] = {
    "12gb": [0],
    "16gb": [0],
    "24gb": [0],
    "32gb": [0, 1],
    "48gb": [0, 1],
    "64gb": [0, 1, 2],
}


@dataclass(frozen=True)
class LlmTierMetadata:
    tier: str
    repo: str
    file: str
    directory: str
    label: str


@dataclass(frozen=True)
class DownloadClient:
    name: str
    path: Path | None


LLM_TIERS: dict[str, LlmTierMetadata] = {
    "12": LlmTierMetadata(
        tier="12",
        repo="unsloth/Qwen3.5-9B-GGUF",
        file="Qwen3.5-9B-Q5_K_M.gguf",
        directory="Qwen3.5-9B-Q5_K_M",
        label="Qwen3.5-9B Q5_K_M (192K, ~6,2 Go)",
    ),
    "16": LlmTierMetadata(
        tier="16",
        repo="unsloth/Qwen3.5-9B-GGUF",
        file="Qwen3.5-9B-Q6_K.gguf",
        directory="Qwen3.5-9B-Q6_K",
        label="Qwen3.5-9B Q6_K (256K, ~7 Go)",
    ),
    "24": LlmTierMetadata(
        tier="24",
        repo="unsloth/Qwen3.6-35B-A3B-GGUF",
        file="Qwen3.6-35B-A3B-UD-IQ4_NL_XL.gguf",
        directory="Qwen3.6-35B-A3B-UD-IQ4_NL_XL",
        label="Qwen3.6-35B-A3B UD-IQ4_NL_XL (256K, ~19 Go — mono-GPU 24 Go)",
    ),
    "32": LlmTierMetadata(
        tier="32",
        repo="unsloth/Qwen3.6-27B-GGUF",
        file="Qwen3.6-27B-Q5_K_M.gguf",
        directory="Qwen3.6-27B-Q5_K_M",
        label="Qwen3.6-27B Q5_K_M (192K, ~19 Go)",
    ),
    "48": LlmTierMetadata(
        tier="48",
        repo="unsloth/Qwen3.6-35B-A3B-GGUF",
        file="Qwen3.6-35B-A3B-UD-Q6_K.gguf",
        directory="Qwen3.6-35B-A3B-UD-Q6_K",
        label="Qwen3.6-35B-A3B UD-Q6_K (256K, ~28 Go)",
    ),
    "64": LlmTierMetadata(
        tier="64",
        repo="unsloth/Qwen3.6-35B-A3B-GGUF",
        file="Qwen3.6-35B-A3B-UD-Q8_K_XL.gguf",
        directory="Qwen3.6-35B-A3B-UD-Q8_K_XL",
        label="Qwen3.6-35B-A3B UD-Q8_K_XL (256K, ~38,5 Go)",
    ),
}


def recommend_tier(total_vram_mb: int) -> str:
    """Recommande un palier LLM depuis la VRAM totale, avec marge de sécurité."""
    if total_vram_mb >= 60000:
        return "64"
    if total_vram_mb >= 46000:
        return "48"
    if total_vram_mb >= 31000:
        return "32"
    if total_vram_mb >= 23000:
        return "24"
    if total_vram_mb >= 15500:
        return "16"
    if total_vram_mb >= 11500:
        return "12"
    return "0"


def get_tier_metadata(tier: str) -> LlmTierMetadata:
    try:
        return LLM_TIERS[tier]
    except KeyError as exc:
        raise ValueError(f"palier LLM inconnu : {tier}") from exc


def render_tier_metadata_shell(tier: str) -> str:
    """Rend les métadonnées d'un palier sous forme d'affectations shell filtrables."""
    metadata = get_tier_metadata(tier)
    return "\n".join(
        [
            f"LLM_REPO={_shell_quote(metadata.repo)}",
            f"LLM_FILE={_shell_quote(metadata.file)}",
            f"LLM_DIR={_shell_quote(metadata.directory)}",
            f"LLM_LABEL={_shell_quote(metadata.label)}",
            "",
        ]
    )


def select_download_client() -> DownloadClient:
    """Sélectionne le client HuggingFace préféré sans lancer de téléchargement."""
    match = first_available(["hf", "huggingface-cli"])
    return DownloadClient(name=match.name if match else "", path=match.path if match else None)


def render_download_client_shell(client: DownloadClient) -> str:
    """Rend le client de téléchargement LLM sous forme d'affectations shell filtrables."""
    return "\n".join(
        [
            f"LLM_HF_DL={_shell_quote(client.name)}",
            f"LLM_HF_DL_PATH={_shell_quote(str(client.path or ''))}",
            "",
        ]
    )


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _default_per_gpu(vram_mb: int, gpu_indices: list[int]) -> list[int]:
    base = vram_mb // len(gpu_indices)
    per_gpu = [base for _ in gpu_indices]
    per_gpu[-1] += vram_mb - sum(per_gpu)
    return per_gpu


def find_profile(repo_root: Path, tier: str) -> Path:
    profiles_dir = repo_root / "scripts" / "arbitrage_profiles"
    matches = sorted(profiles_dir.glob(f"{tier}_*.sh"))
    if not matches:
        raise FileNotFoundError(f"aucun profil pour le palier {tier} dans {profiles_dir}")
    return matches[0]


def render_wrapper(
    *,
    profile_path: Path,
    models_dir: str | None = None,
    llama_server: str | None = None,
    gpu_indices: list[int],
) -> str:
    lines = [
        "#!/usr/bin/env bash",
        "# Fichier généré localement par transcria.install_arbitrage.",
        "# Ne pas versionner : modifier la source dans scripts/arbitrage_profiles/ ou régénérer.",
        "set -euo pipefail",
    ]
    if models_dir:
        lines.append('if [[ -z "${MODELS_DIR:-}" ]]; then')
        lines.append(f"  export MODELS_DIR={_shell_quote(models_dir)}")
        lines.append("fi")
    if llama_server:
        lines.append('if [[ -z "${LLAMA_SERVER:-}" ]]; then')
        lines.append(f"  export LLAMA_SERVER={_shell_quote(llama_server)}")
        lines.append("fi")
    lines.append(f"export ARBITRAGE_GPU=\"${{ARBITRAGE_GPU:-{','.join(str(i) for i in gpu_indices)}}}\"")
    lines.append(f"exec {_shell_quote(str(profile_path))} \"$@\"")
    return "\n".join(lines) + "\n"


def write_wrapper(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    current_mode = path.stat().st_mode
    path.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def apply_profile(
    *,
    repo_root: Path,
    config_path: Path,
    tier: str,
    models_dir: str | None = None,
    llama_server: str | None = None,
    output_path: Path | None = None,
) -> Path:
    if tier not in TIER_VRAM_MB:
        raise ValueError(f"palier inconnu: {tier}")
    repo_root = repo_root.resolve()
    config_path = config_path.resolve()
    profile_path = find_profile(repo_root, tier).resolve()
    gpu_indices = TIER_GPU_INDICES[tier]
    output_path = output_path or repo_root / "scripts" / "generated" / "launch_arbitrage.local.sh"
    output_path = output_path.resolve()

    write_wrapper(
        output_path,
        render_wrapper(
            profile_path=profile_path,
            models_dir=models_dir,
            llama_server=llama_server,
            gpu_indices=gpu_indices,
        ),
    )
    set_yaml_file_value(config_path, "services.arbitrage_script", str(output_path))
    apply_gpu_calibration(
        config_path,
        vram_mb=TIER_VRAM_MB[tier],
        gpu_indices=gpu_indices,
        vram_mb_per_gpu=_default_per_gpu(TIER_VRAM_MB[tier], gpu_indices),
    )
    return output_path


def status(*, repo_root: Path, config_path: Path) -> list[str]:
    cfg = load_yaml_file(config_path)
    script = get_yaml_value(cfg, "services.arbitrage_script") or "./scripts/launch_arbitrage.sh"
    lines = [f"services.arbitrage_script: {script}"]
    script_path = Path(str(script))
    if not script_path.is_absolute():
        script_path = repo_root / script_path
    if script_path.exists():
        lines.append(f"script existe: {script_path}")
    else:
        lines.append(f"script introuvable: {script_path}")
    return lines


def render_setup_log(
    *,
    event: str,
    value: str = "",
    profile: str = "",
    gpu_count: str = "",
    max_mb: str = "",
    tier: str = "",
    label: str = "",
) -> str:
    """Rend les messages de sélection de la LLM d'arbitrage locale."""
    if event == "profile-skipped":
        return f"INFO:Profil {profile} : LLM d'arbitrage locale non requise\n"
    if event == "vram-too-low":
        return f"WARN:VRAM totale {value} Mio (< 12 Go) — pas de LLM d'arbitrage local.\n"
    if event == "raw-mode":
        return "INFO:TranscrIA fonctionnera en TRANSCRIPTION BRUTE (résumé/correction LLM désactivés).\n"
    if event == "opencode-missing":
        return "WARN:opencode absent — LLM d'arbitrage non configurable (transcription brute).\n"
    if event == "opencode-install-later":
        return "INFO:Installez opencode puis relancez, ou utilisez scripts/switch_arbitrage_llm.sh plus tard.\n"
    if event == "vram-status":
        return f"OK:VRAM : total {value} Mio sur {gpu_count} GPU (plus grande carte {max_mb} Mio)\n"
    if event == "planner-fallback":
        return "WARN:Planner de placement indisponible — recommandation par VRAM totale (moins fiable).\n"
    if event == "no-tier":
        return "WARN:Aucun palier LLM ne tient sur cette topologie — transcription brute conseillée.\n"
    if event == "recommended-tier":
        return f"INFO:Palier recommandé : {tier} Go → {label}\n"
    if event == "tiers-info":
        return "INFO:Paliers : 12 / 16 / 24 / 32 / 48 / 64 (Go) — laisser vide pour ignorer.\n"
    if event == "llama-qualified":
        return f"OK:llama-server qualifié : {value} (build {tier}, source {label})\n"
    if event == "llama-unusable":
        return f"WARN:llama-server trouvé mais NON utilisable ({tier}) : {value}\n"
    if event == "llama-ld-hint":
        return (
            "WARN:Libs llama hors chemins standard — exportez "
            f"LLAMA_LD_LIBRARY_PATH={value} dans l'environnement du service (les profils l'honorent).\n"
        )
    if event == "model-present":
        return f"OK:Modèle déjà présent : {value}\n"
    if event == "hf-cli-missing":
        return "ERROR:Ni 'hf' ni 'huggingface-cli' trouvés — installez : pip install -U huggingface_hub\n"
    if event == "download-start":
        return f"INFO:Téléchargement ({tier}) de {value} → {label} (peut prendre plusieurs minutes)…\n"
    if event == "model-downloaded":
        return f"OK:Modèle téléchargé : {value}\n"
    if event == "download-failed":
        return "ERROR:Téléchargement échoué — vérifiez la connectivité / le HF_TOKEN.\n"
    if event == "download-skipped":
        return "INFO:Téléchargement ignoré.\n"
    if event == "tier-activated":
        return f"OK:Palier {tier} Go activé (alias générique 'arbitrage').\n"
    if event == "calibration-ok":
        return "OK:Calibration GPU écrite (placement réel par carte).\n"
    if event == "calibration-failed":
        return "WARN:Calibration auto échouée — vérifiez : scripts/check_arbitrage_llm.sh\n"
    if event == "start-managed":
        return "INFO:Démarrage de la LLM : géré par TranscrIA via services.arbitrage_script.\n"
    if event == "switch-incomplete":
        return f"WARN:Bascule de palier incomplète — voir scripts/switch_arbitrage_llm.sh {tier}gb\n"
    if event == "model-absent":
        return "INFO:Modèle absent — palier non activé (transcription brute pour l'instant).\n"
    if event == "ignored":
        return "INFO:LLM d'arbitrage ignoré — transcription brute. Activable plus tard :\n"
    if event == "manual-switch":
        return "INFO:  scripts/switch_arbitrage_llm.sh <palier>  (après téléchargement du modèle)\n"
    raise ValueError(f"événement LLM inconnu : {event}")


def render_prompt(*, prompt: str, label: str = "", repo: str = "") -> str:
    """Rend les questions interactives du choix de LLM d'arbitrage."""
    if prompt == "tier":
        return "Palier LLM à installer"
    if prompt == "models-dir":
        return "Répertoire de téléchargement des modèles"
    if prompt == "llama-server":
        return "Chemin du binaire llama-server (≥ b9630 — voir scripts/detect_llama_server.py)"
    if prompt == "download":
        return f"Télécharger {label} depuis {repo} ?"
    raise ValueError(f"prompt LLM inconnu : {prompt}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Génère le wrapper local de LLM d'arbitrage TranscrIA.")
    parser.add_argument("tier", nargs="?", choices=(*TIER_VRAM_MB.keys(), "status"), default="status")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--models-dir", default=None)
    parser.add_argument("--llama-server", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--setup-log", action="store_true", help="rend un message de sélection LLM")
    parser.add_argument("--event", default="")
    parser.add_argument("--value", default="")
    parser.add_argument("--profile", default="")
    parser.add_argument("--gpu-count", default="")
    parser.add_argument("--max-mb", default="")
    parser.add_argument("--tier-value", default="")
    parser.add_argument("--label", default="")
    parser.add_argument("--prompt", default="")
    parser.add_argument("--repo", default="")
    parser.add_argument("--recommend-tier", action="store_true", help="recommande un palier depuis --total-vram-mb")
    parser.add_argument("--tier-info", action="store_true", help="rend les métadonnées shell d'un palier")
    parser.add_argument("--download-client", action="store_true", help="rend le client HuggingFace disponible pour télécharger la LLM")
    parser.add_argument("--total-vram-mb", type=int, default=0)
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root)
    config_path = Path(args.config)
    try:
        if args.setup_log:
            if not args.event:
                print("--event requis avec --setup-log", file=sys.stderr)
                return 2
            print(
                render_setup_log(
                    event=args.event,
                    value=args.value,
                    profile=args.profile,
                    gpu_count=args.gpu_count,
                    max_mb=args.max_mb,
                    tier=args.tier_value,
                    label=args.label,
                ),
                end="",
            )
            return 0
        if args.prompt:
            print(render_prompt(prompt=args.prompt, label=args.label, repo=args.repo), end="")
            return 0
        if args.recommend_tier:
            print(recommend_tier(args.total_vram_mb))
            return 0
        if args.tier_info:
            if not args.tier_value:
                print("--tier-value requis avec --tier-info", file=sys.stderr)
                return 2
            print(render_tier_metadata_shell(args.tier_value), end="")
            return 0
        if args.download_client:
            print(render_download_client_shell(select_download_client()), end="")
            return 0
        if args.tier == "status":
            for line in status(repo_root=repo_root, config_path=config_path):
                print(line)
            return 0
        output = apply_profile(
            repo_root=repo_root,
            config_path=config_path,
            tier=args.tier,
            models_dir=args.models_dir,
            llama_server=args.llama_server,
            output_path=Path(args.output) if args.output else None,
        )
        print(f"wrapper généré: {output}")
        print(f"config.yaml: services.arbitrage_script={output}")
        print(f"config.yaml: gpu.llm_vram_mb={TIER_VRAM_MB[args.tier]} ; gpu.llm_gpu_indices={TIER_GPU_INDICES[args.tier]}")
        return 0
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

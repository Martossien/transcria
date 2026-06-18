from __future__ import annotations

import argparse
import stat
import sys
from pathlib import Path

from transcria.config.gpu_calibration import apply_gpu_calibration
from transcria.config.yaml_file import get_yaml_value, load_yaml_file, set_yaml_file_value

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
    raise ValueError(f"événement LLM inconnu : {event}")


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

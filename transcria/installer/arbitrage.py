from __future__ import annotations

import argparse
import hashlib
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from transcria.config.gpu_calibration import apply_gpu_calibration
from transcria.config.llm_profiles import load_llm_profiles, select_profile
from transcria.config.yaml_file import get_yaml_value, load_yaml_file, set_yaml_file_value
from transcria.gpu.llm_footprint import derive_footprint_mb, read_gguf_arch
from transcria.gpu.llm_placement import (
    DEFAULT_SAFETY_MARGIN_MB,
    Placement,
    plan_for_tier,
    recommend,
)
from transcria.install_messages import t
from transcria.installer.prerequisites import first_available

# Paliers extraits vers installer/tiers.py (vague C6) — ré-exportés ici : les
# consommateurs historiques (models_catalog, entrypoint, tests) importaient chez nous.
from transcria.installer.tiers import (  # noqa: F401 — ré-exports
    LLM_TIERS,
    TIER_GPU_INDICES,
    TIER_VRAM_MB,
    LlmTierMetadata,
    _build_llamacpp_tables,
    _llamacpp_engine,
    get_tier_metadata,
    recommend_tier,
)


@dataclass(frozen=True)
class DownloadClient:
    name: str
    path: Path | None


@dataclass(frozen=True)
class LlamaFallback:
    server: Path | None


@dataclass(frozen=True)
class PlacementRecommendation:
    tier: str
    planner_fallback: bool
    feasible: bool
    warnings: tuple[str, ...] = ()


# Octets/élément du KV llama.cpp (cache-type q8_0) — pour la dérivation d'empreinte.
_LLAMACPP_KV_BYTES: int = int(_llamacpp_engine().get("kv_dtype_bytes", 1))


def parse_gpu_sizes_csv(value: str) -> list[int]:
    sizes: list[int] = []
    for token in value.replace(",", " ").split():
        if not token.isdigit():
            raise ValueError(f"taille GPU invalide : {token}")
        size = int(token)
        if size <= 0:
            raise ValueError(f"taille GPU invalide : {token}")
        sizes.append(size)
    return sizes


def recommend_placement_tier(*, gpu_sizes_csv: str, total_vram_mb: int) -> PlacementRecommendation:
    """Recommande un palier en privilégiant le placement réel par carte."""
    sizes = parse_gpu_sizes_csv(gpu_sizes_csv)
    if sizes:
        placement = recommend(sizes, safety_margin_mb=DEFAULT_SAFETY_MARGIN_MB)
        tier = str(placement.tier_gb) if placement.feasible and placement.tier_gb else ""
        return PlacementRecommendation(
            tier=tier,
            planner_fallback=False,
            feasible=placement.feasible,
            warnings=tuple(placement.warnings),
        )

    tier = recommend_tier(total_vram_mb)
    return PlacementRecommendation(
        tier="" if tier == "0" else tier,
        planner_fallback=True,
        feasible=tier != "0",
    )


def render_placement_recommendation_shell(recommendation: PlacementRecommendation) -> str:
    return "\n".join(
        [
            f"LLM_REC_TIER={_shell_quote(recommendation.tier)}",
            f"LLM_PLANNER_FALLBACK={1 if recommendation.planner_fallback else 0}",
            f"LLM_PLACEMENT_FEASIBLE={1 if recommendation.feasible else 0}",
            "",
        ]
    )


def emit_placement_warnings(placement: PlacementRecommendation | Placement) -> None:
    for warning in placement.warnings:
        print(f"  ⚠ {warning}", file=sys.stderr)


def apply_placement_calibration(*, gpu_sizes_csv: str, tier: str, config_path: Path) -> Placement:
    sizes = parse_gpu_sizes_csv(gpu_sizes_csv)
    if not sizes:
        raise ValueError("aucune taille GPU fournie pour la calibration")
    placement = plan_for_tier(int(tier), sizes, safety_margin_mb=DEFAULT_SAFETY_MARGIN_MB)
    if not placement.feasible:
        raise RuntimeError(placement.reason)
    apply_gpu_calibration(
        config_path,
        vram_mb=placement.vram_mb,
        gpu_indices=placement.gpu_indices,
        vram_mb_per_gpu=placement.vram_mb_per_gpu,
    )
    return placement


def render_tier_metadata_shell(tier: str) -> str:
    """Rend les métadonnées d'un palier sous forme d'affectations shell filtrables."""
    metadata = get_tier_metadata(tier)
    return "\n".join(
        [
            f"LLM_REPO={_shell_quote(metadata.repo)}",
            f"LLM_FILE={_shell_quote(metadata.file)}",
            f"LLM_DIR={_shell_quote(metadata.directory)}",
            f"LLM_LABEL={_shell_quote(metadata.label)}",
            f"LLM_CONTEXT={metadata.context}",
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


def select_llama_fallback(*, user_home: Path) -> LlamaFallback:
    """Sélectionne un fallback llama-server simple si le détecteur avancé ne trouve rien."""
    match = first_available(["llama-server"])
    candidates = [
        match.path if match else None,
        Path(user_home) / "llama.cpp" / "build" / "bin" / "llama-server",
        Path("/usr/local/bin/llama-server"),
    ]
    for candidate in candidates:
        if candidate and candidate.is_file() and candidate.stat().st_mode & 0o111:
            return LlamaFallback(server=candidate)
    return LlamaFallback(server=None)


def render_llama_fallback_shell(fallback: LlamaFallback) -> str:
    """Rend le fallback llama-server sous forme d'affectation shell filtrable."""
    return f"LLAMA_FALLBACK={_shell_quote(str(fallback.server or ''))}\n"


def render_vllm_env_shell(choice: object | None) -> str:
    """Rend l'env vLLM (modèle/TP/contexte) résolu depuis le catalogue, filtrable.

    `choice` = ProfileChoice de select_profile('vllm', …) ou None (aucun palier ne tient)."""
    if choice is None:
        return "ARBITRAGE_MODEL=\nARBITRAGE_TP=\nARBITRAGE_MAX_LEN=\n"
    return "\n".join(
        [
            f"ARBITRAGE_MODEL={_shell_quote(str(choice.model))}",       # type: ignore[attr-defined]
            f"ARBITRAGE_TP={choice.tp or 1}",                            # type: ignore[attr-defined]
            f"ARBITRAGE_MAX_LEN={choice.context}",                       # type: ignore[attr-defined]
            "",
        ]
    )


# ── Niveau 2 : binaire llama.cpp CUDA précompilé (ai-dock/llama.cpp-cuda) ──────
#
# Upstream ggml-org ne publie AUCUN binaire llama-server CUDA pour Linux (vérifié :
# le CUDA n'est publié qu'en Windows). ai-dock/llama.cpp-cuda comble ce manque en suivant
# les releases upstream (artefacts `llama.cpp-b<ID>-cuda-<CUDA>-<arch>.tar.gz`). C'est une
# source TIERCE : on l'utilise en OPT-IN, sur un build ÉPINGLÉ, et avec checksum VÉRIFIÉ.
# Elle évite la compilation (donc `nvcc`) sur distro vierge — son intérêt principal.

AIDOCK_REPO = "ai-dock/llama.cpp-cuda"
AIDOCK_DEFAULT_CUDA = "12.8"
_ARCH_MAP = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
_PREBUILT_RE = re.compile(r"llama\.cpp-b(\d+)-cuda-([0-9.]+)-([a-z0-9]+)\.tar\.gz$")


def normalize_arch(machine: str) -> str:
    """Mappe `uname -m` (x86_64/aarch64) vers la nomenclature ai-dock (amd64/arm64)."""
    return _ARCH_MAP.get(machine.strip().lower(), "amd64")


def prebuilt_artifact_name(build_id: int, *, cuda: str = AIDOCK_DEFAULT_CUDA, arch: str = "amd64") -> str:
    return f"llama.cpp-b{build_id}-cuda-{cuda}-{arch}.tar.gz"


def parse_prebuilt_artifact(name: str) -> tuple[int, str, str] | None:
    """(build_id, cuda, arch) depuis un nom d'artefact, ou None si non conforme."""
    m = _PREBUILT_RE.search(name.strip())
    if not m:
        return None
    return int(m.group(1)), m.group(2), m.group(3)


def select_prebuilt_artifact(
    available: list[str], *, wanted_build: int, cuda: str = AIDOCK_DEFAULT_CUDA, arch: str = "amd64"
) -> str | None:
    """Choisit l'artefact CUDA le plus adapté (politique « nearest »).

    Priorité : build EXACT demandé > plus proche build SUPÉRIEUR (nearest newer) > à défaut
    le plus récent disponible inférieur. Filtre d'abord sur (cuda, arch) — on ne mélange
    JAMAIS les versions CUDA ni les architectures.
    """
    by_build: dict[int, str] = {}
    for name in available:
        parsed = parse_prebuilt_artifact(name)
        if parsed and parsed[1] == cuda and parsed[2] == arch:
            by_build[parsed[0]] = name
    if not by_build:
        return None
    if wanted_build in by_build:
        return by_build[wanted_build]
    newer = sorted(b for b in by_build if b > wanted_build)
    if newer:
        return by_build[newer[0]]
    older = sorted((b for b in by_build if b < wanted_build), reverse=True)
    return by_build[older[0]] if older else None


def sha256_of_file(path: Path, *, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def verify_sha256(path: Path, expected: str) -> bool:
    """Compare le sha256 du fichier à la valeur attendue (insensible à la casse).

    Un expected vide échoue volontairement : pas de vérification = pas de confiance
    (on refuse d'exécuter un binaire tiers non vérifié)."""
    if not expected or not expected.strip():
        return False
    return sha256_of_file(path).lower() == expected.strip().lower()


def install_prebuilt_llama(
    *,
    build_id: int,
    dest_dir: Path,
    expected_sha256: str,
    cuda: str = AIDOCK_DEFAULT_CUDA,
    arch: str | None = None,
) -> Path | None:
    """Télécharge + vérifie + extrait un `llama-server` CUDA précompilé (ai-dock).

    I/O réseau (non couvert par les tests unitaires — la logique de sélection/checksum,
    elle, l'est). Retourne le chemin du binaire `llama-server`, ou None en cas d'échec.
    REFUSE d'installer sans checksum vérifié (source tierce)."""
    import json
    import platform
    import tarfile
    import tempfile
    import urllib.request

    if not expected_sha256 or not expected_sha256.strip():
        print("Refus : binaire tiers sans checksum sha256 à vérifier.", file=sys.stderr)
        return None
    resolved_arch = arch or normalize_arch(platform.machine())
    tag = f"b{build_id}"
    api = f"https://api.github.com/repos/{AIDOCK_REPO}/releases/tags/{tag}"
    try:
        with urllib.request.urlopen(api, timeout=30) as resp:  # noqa: S310 — URL constante (GitHub API)
            release = json.load(resp)
        names = [a.get("name", "") for a in release.get("assets", [])]
        chosen = select_prebuilt_artifact(names, wanted_build=build_id, cuda=cuda, arch=resolved_arch)
        if not chosen:
            print(f"Aucun artefact CUDA {cuda}/{resolved_arch} dans la release {tag}.", file=sys.stderr)
            return None
        url = next(a["browser_download_url"] for a in release["assets"] if a.get("name") == chosen)
    except Exception as exc:  # noqa: BLE001 — best-effort réseau
        print(f"Échec récupération release ai-dock {tag} : {exc}", file=sys.stderr)
        return None

    dest_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / chosen
        try:
            urllib.request.urlretrieve(url, archive)  # noqa: S310 — URL de release GitHub
        except Exception as exc:  # noqa: BLE001
            print(f"Échec téléchargement {chosen} : {exc}", file=sys.stderr)
            return None
        if not verify_sha256(archive, expected_sha256):
            print(f"Checksum sha256 INVALIDE pour {chosen} — binaire rejeté.", file=sys.stderr)
            return None
        with tarfile.open(archive, "r:gz") as tf:
            tf.extractall(dest_dir)  # noqa: S202 — archive vérifiée par checksum

    for candidate in dest_dir.rglob("llama-server"):
        if candidate.is_file():
            candidate.chmod(candidate.stat().st_mode | 0o111)
            return candidate
    print(f"llama-server introuvable après extraction dans {dest_dir}.", file=sys.stderr)
    return None


def run_llama_detector(*, repo_root: Path, python_bin: str = sys.executable) -> tuple[str, str]:
    """Exécute le détecteur avancé llama-server sans rendre l'installation bloquante."""
    detector = Path(repo_root) / "scripts" / "detect_llama_server.py"
    try:
        result = subprocess.run(
            [python_bin, str(detector), "--format", "shell"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "", f"{exc}\n"
    return result.stdout, result.stderr


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
        "# Fichier généré localement par transcria.installer.arbitrage.",
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
    # Réservation VRAM = empreinte DÉRIVÉE (poids GGUF réels + KV du contexte) si le modèle
    # est déjà téléchargé ; sinon repli sur le budget de palier. JAMAIS une taille en dur.
    vram_mb = _derive_llamacpp_vram_mb(tier, models_dir) or TIER_VRAM_MB[tier]
    apply_gpu_calibration(
        config_path,
        vram_mb=vram_mb,
        gpu_indices=gpu_indices,
        vram_mb_per_gpu=_default_per_gpu(vram_mb, gpu_indices),
    )
    return output_path


def _derive_llamacpp_vram_mb(tier: str, models_dir: str | None) -> int | None:
    """Empreinte VRAM dérivée du GGUF réel (poids + KV calculé). None si absent/illisible.

    ``tier`` peut être ``"24"`` ou ``"24gb"`` (LLM_TIERS est clé sans suffixe)."""
    if not models_dir:
        return None

    tid = tier[:-2] if tier.endswith("gb") else tier
    meta = LLM_TIERS.get(tid)
    if meta is None:
        return None
    gguf = Path(models_dir).expanduser() / meta.directory / meta.file
    if not gguf.is_file():
        return None
    return derive_footprint_mb(
        model_path=gguf, arch=read_gguf_arch(gguf),
        context=meta.context, kv_dtype_bytes=_LLAMACPP_KV_BYTES,
    )


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
    """Rend les messages de sélection de la LLM d'arbitrage locale (FR/EN ; préfixe et
    lignes de commande scripts/*.sh non localisés)."""

    if event == "profile-skipped":
        return f"INFO:{t('arb_profile_skipped', profile=profile)}\n"
    if event == "vram-too-low":
        return f"WARN:{t('arb_vram_too_low', value=value)}\n"
    if event == "raw-mode":
        return f"INFO:{t('arb_raw_mode')}\n"
    if event == "opencode-missing":
        return f"WARN:{t('arb_opencode_missing')}\n"
    if event == "opencode-install-later":
        return f"INFO:{t('arb_opencode_install_later')}\n"
    if event == "vram-status":
        return f"OK:{t('arb_vram_status', value=value, gpu_count=gpu_count, max_mb=max_mb)}\n"
    if event == "planner-fallback":
        return f"WARN:{t('arb_planner_fallback')}\n"
    if event == "no-tier":
        return f"WARN:{t('arb_no_tier')}\n"
    if event == "recommended-tier":
        return f"INFO:{t('arb_recommended_tier', tier=tier, label=label)}\n"
    if event == "tiers-info":
        return f"INFO:{t('arb_tiers_info')}\n"
    if event == "llama-qualified":
        return f"OK:{t('arb_llama_qualified', value=value, tier=tier, label=label)}\n"
    if event == "llama-unusable":
        return f"WARN:{t('arb_llama_unusable', tier=tier, value=value)}\n"
    if event == "llama-ld-hint":
        return f"WARN:{t('arb_llama_ld_hint', value=value)}\n"
    if event == "model-present":
        return f"OK:{t('arb_model_present', value=value)}\n"
    if event == "hf-cli-missing":
        return f"ERROR:{t('arb_hf_cli_missing')}\n"
    if event == "download-start":
        return f"INFO:{t('arb_download_start', tier=tier, value=value, label=label)}\n"
    if event == "model-downloaded":
        return f"OK:{t('arb_model_downloaded', value=value)}\n"
    if event == "download-failed":
        return f"ERROR:{t('arb_download_failed')}\n"
    if event == "download-skipped":
        return f"INFO:{t('arb_download_skipped')}\n"
    if event == "tier-activated":
        return f"OK:{t('arb_tier_activated', tier=tier)}\n"
    if event == "calibration-ok":
        return f"OK:{t('arb_calibration_ok')}\n"
    if event == "calibration-failed":
        return f"WARN:{t('arb_calibration_failed')}\n"
    if event == "start-managed":
        return f"INFO:{t('arb_start_managed')}\n"
    if event == "switch-incomplete":
        return f"WARN:{t('arb_switch_incomplete', tier=tier)}\n"
    if event == "model-absent":
        return f"INFO:{t('arb_model_absent')}\n"
    if event == "ignored":
        return f"INFO:{t('arb_ignored')}\n"
    if event == "manual-switch":
        return "INFO:  scripts/switch_arbitrage_llm.sh <palier>  (après téléchargement du modèle)\n"
    raise ValueError(f"événement LLM inconnu : {event}")


def render_prompt(*, prompt: str, label: str = "", repo: str = "") -> str:
    """Rend les questions interactives du choix de LLM d'arbitrage (FR/EN)."""

    if prompt == "tier":
        return t("arb_prompt_tier")
    if prompt == "models-dir":
        return t("arb_prompt_models_dir")
    if prompt == "llama-server":
        return t("arb_prompt_llama")
    if prompt == "download":
        return t("arb_prompt_download", label=label, repo=repo)
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
    parser.add_argument("--llama-detect", action="store_true", help="lance le détecteur avancé llama-server")
    parser.add_argument("--llama-fallback", action="store_true", help="rend un fallback llama-server simple")
    parser.add_argument("--placement-plan", action="store_true", help="rend la recommandation de placement LLM")
    parser.add_argument("--apply-placement-calibration", action="store_true", help="écrit la calibration GPU depuis le placement LLM")
    parser.add_argument("--gpu-sizes-csv", default="")
    parser.add_argument("--user-home", default="")
    parser.add_argument("--total-vram-mb", type=int, default=0)
    parser.add_argument("--vllm-env", action="store_true",
                        help="rend l'env vLLM (modèle/TP/max_len) résolu depuis le catalogue selon le matériel")
    parser.add_argument("--install-llama-prebuilt", action="store_true",
                        help="télécharge un llama-server CUDA précompilé (ai-dock), vérifie le checksum, extrait")
    parser.add_argument("--llama-build", type=int, default=0, help="build upstream épinglé (bXXXX) pour --install-llama-prebuilt")
    parser.add_argument("--dest", default="", help="dossier vendor de destination du binaire précompilé")
    parser.add_argument("--sha256", default="", help="checksum sha256 attendu de l'archive précompilée")
    parser.add_argument("--cuda", default=AIDOCK_DEFAULT_CUDA, help="version CUDA de l'artefact précompilé")
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
        if args.llama_detect:
            stdout, stderr = run_llama_detector(repo_root=repo_root)
            print(stdout, end="")
            print(stderr, end="", file=sys.stderr)
            return 0
        if args.vllm_env:
            gpu_count = int(args.gpu_count) if str(args.gpu_count).isdigit() else 1
            choice = select_profile(
                load_llm_profiles(), "vllm",
                gpu_count=gpu_count, per_card_vram_mb=0, total_vram_mb=args.total_vram_mb,
            )
            print(render_vllm_env_shell(choice), end="")
            return 0
        if args.llama_fallback:
            if not args.user_home:
                print("--user-home requis avec --llama-fallback", file=sys.stderr)
                return 2
            print(render_llama_fallback_shell(select_llama_fallback(user_home=Path(args.user_home))), end="")
            return 0
        if args.install_llama_prebuilt:
            if not args.llama_build or not args.dest or not args.sha256:
                print("--llama-build, --dest et --sha256 requis avec --install-llama-prebuilt", file=sys.stderr)
                return 2
            server = install_prebuilt_llama(
                build_id=args.llama_build,
                dest_dir=Path(args.dest),
                expected_sha256=args.sha256,
                cuda=args.cuda,
            )
            if not server:
                return 1
            print(f"LLAMA_PREBUILT={_shell_quote(str(server))}\n", end="")
            return 0
        if args.placement_plan:
            recommendation = recommend_placement_tier(gpu_sizes_csv=args.gpu_sizes_csv, total_vram_mb=args.total_vram_mb)
            print(render_placement_recommendation_shell(recommendation), end="")
            emit_placement_warnings(recommendation)
            return 0
        if args.apply_placement_calibration:
            if not args.tier_value:
                print("--tier-value requis avec --apply-placement-calibration", file=sys.stderr)
                return 2
            placement = apply_placement_calibration(gpu_sizes_csv=args.gpu_sizes_csv, tier=args.tier_value, config_path=config_path)
            emit_placement_warnings(placement)
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
        print(f"config.yaml: gpu.llm_vram_mb calibré (empreinte dérivée si GGUF présent, sinon budget {TIER_VRAM_MB[args.tier]}) ; "
              f"gpu.llm_gpu_indices={TIER_GPU_INDICES[args.tier]}")
        return 0
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

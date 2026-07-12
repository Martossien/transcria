"""Phase installeur : runtime STT servi audio.cpp (backend `qwen3asr`).

Provisionne, dans ``<runtimes_dir>/audiocpp/`` :
  - ``src/``  : checkout git ÉPINGLÉ (commit précis — jamais un main flottant :
    audio.cpp est jeune et bouge vite, on a vu un bug de session corrigé en un
    jour ; l'épinglage rend l'install reproductible) ;
  - ``bin/audiocpp_server`` : compilé CUDA (cmake) ;
  - ``venv/`` : venv dédié aux outils (tools/model_manager.py — torch CPU),
    isolé du venv projet ;
  - ``etc/``  : configs serveur générées par le lanceur (voir
    ``scripts/launch_stt_qwen3asr.sh`` + ``audiocpp_server_config``) ;
  - ``COMMIT`` : marqueur d'idempotence (SHA effectivement construit).

Opt-in : ni ``install.sh`` ni les images ne l'exécutent par défaut —
``python -m transcria.installer.cli audiocpp`` (opérateur). Même patron que
``moss_site_phase`` : plan figé, runner injecté, erreurs typées, idempotent.
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

# Commit épinglé : inclut le fix de session Nemotron (858074a) et le support
# Qwen3-ASR-1.7B HF (#43) — l'état QUALIFIÉ sur notre benchmark (0,421 WER).
AUDIOCPP_REPO = "https://github.com/0xShug0/audio.cpp"
AUDIOCPP_PINNED_COMMIT = "3982dfb29ff0b37a26b799045f85732b20a0bdef"
# Modèle recommandé (Apache-2.0, ~3,9 Go) — id du paquet dans LEUR model_manager.
AUDIOCPP_DEFAULT_MODEL_PACKAGE = "qwen3_asr_1_7b_hf"
AUDIOCPP_DEFAULT_MODEL_DIR = "Qwen3-ASR-1.7B-hf"


class Runner(Protocol):
    def __call__(self, cmd: list[str], *, cwd: str | None = None) -> None: ...


class AudiocppPhaseError(RuntimeError):
    """Échec de provisionnement du runtime audio.cpp."""


@dataclass(frozen=True)
class AudiocppPlan:
    runtimes_dir: Path
    commit: str = AUDIOCPP_PINNED_COMMIT
    with_model: bool = False
    force: bool = False
    jobs: int = 0  # 0 = nproc
    # « native » = l'arch du GPU de CETTE machine (installeur local). PIÈGE vécu :
    # sans ce réglage, cmake retombait sur 75 → kernels ggml compilés pour compute
    # 7.5 ⇒ SIGABRT (ggml_cuda_error) à la première inférence sur une RTX 3090 (8.6).
    cuda_archs: str = "native"


def resolve_runtimes_dir(default: str | Path = "./runtimes") -> Path:
    """Racine des runtimes servis — surchargeable (Docker : /opt/runtimes)."""
    return Path(os.environ.get("TRANSCRIA_RUNTIMES_DIR") or default)


def audiocpp_home(runtimes_dir: Path) -> Path:
    return runtimes_dir / "audiocpp"


def audiocpp_is_complete(home: Path, commit: str) -> bool:
    binary = home / "bin" / "audiocpp_server"
    marker = home / "COMMIT"
    return (
        binary.is_file()
        and os.access(binary, os.X_OK)
        and marker.is_file()
        and marker.read_text(encoding="utf-8").strip() == commit
    )


def audiocpp_server_config(
    *, port: int, device: int = 0, host: str = "0.0.0.0",
    model_id: str = "qwen3-asr-1.7b",
    model_path: str | Path = "",
) -> dict:
    """Config JSON du serveur audio.cpp — helper PUR et testable (le lanceur bash
    l'appelle via `python -m transcria.installer.audiocpp_phase --emit-config`).

    `device` est un index RELATIF : le lanceur exporte CUDA_VISIBLE_DEVICES=STT_GPU,
    donc le seul GPU visible du serveur est toujours 0."""
    return {
        "host": host,
        "port": int(port),
        "backend": "cuda",
        "device": int(device),
        "models": [{
            "id": str(model_id),
            "family": "qwen3_asr",  # underscore — "qwen3-asr" est rejeté (family hint)
            "path": str(model_path),
            "task": "asr",
            "mode": "offline",
        }],
    }


def apply_audiocpp(plan: AudiocppPlan, *, console, runner: Runner) -> None:
    home = audiocpp_home(plan.runtimes_dir.expanduser())
    if not plan.force and audiocpp_is_complete(home, plan.commit):
        console.ok(f"audio.cpp déjà provisionné : {home} (commit {plan.commit[:12]} — --force pour reconstruire)")
        return

    src = home / "src"
    bin_dir = home / "bin"
    venv_dir = home / "venv"
    (home / "etc").mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)

    # 1) Sources épinglées (clone frais si le checkout ne correspond pas / --force).
    if plan.force and src.exists():
        shutil.rmtree(src)
    if not (src / ".git").exists():
        console.info(f"Clone audio.cpp → {src}")
        try:
            runner(["git", "clone", AUDIOCPP_REPO, str(src)])
        except Exception as exc:  # noqa: BLE001 — remonté en erreur typée
            raise AudiocppPhaseError(f"clone audio.cpp échoué : {exc}") from exc
    try:
        runner(["git", "-C", str(src), "fetch", "origin", plan.commit])
        runner(["git", "-C", str(src), "checkout", plan.commit])
    except Exception as exc:  # noqa: BLE001
        raise AudiocppPhaseError(f"checkout du commit épinglé {plan.commit[:12]} échoué : {exc}") from exc

    # 2) Compilation CUDA (audiocpp_server uniquement).
    jobs = plan.jobs or (os.cpu_count() or 4)
    console.info(f"Compilation audiocpp_server (CUDA, -j{jobs}) — plusieurs minutes…")
    try:
        runner(["cmake", "-S", str(src), "-B", str(src / "build"),
                "-DCMAKE_BUILD_TYPE=Release", "-DGGML_CUDA=ON",
                f"-DCMAKE_CUDA_ARCHITECTURES={plan.cuda_archs}"])
        runner(["cmake", "--build", str(src / "build"), "-j", str(jobs),
                "--target", "audiocpp_server"])
    except Exception as exc:  # noqa: BLE001
        raise AudiocppPhaseError(f"compilation audio.cpp échouée : {exc}") from exc
    built = src / "build" / "bin" / "audiocpp_server"
    if not built.is_file():
        raise AudiocppPhaseError(f"binaire absent après compilation : {built}")
    shutil.copy2(built, bin_dir / "audiocpp_server")

    # 3) Venv outils (model_manager — torch CPU, jamais le venv projet).
    if not (venv_dir / "bin" / "python").exists():
        console.info("Venv outils audio.cpp (model_manager, torch CPU)…")
        try:
            runner(["python3", "-m", "venv", str(venv_dir)])
            runner([str(venv_dir / "bin" / "pip"), "install", "--quiet",
                    "torch", "--index-url", "https://download.pytorch.org/whl/cpu"])
            # Deps de tools/model_manager.py (pas de requirements.txt en amont —
            # liste relevée sur le commit épinglé : hub + conversion safetensors).
            runner([str(venv_dir / "bin" / "pip"), "install", "--quiet",
                    "huggingface_hub", "safetensors", "numpy", "pyyaml", "tqdm"])
        except Exception as exc:  # noqa: BLE001
            raise AudiocppPhaseError(f"venv outils audio.cpp échoué : {exc}") from exc

    # 4) Modèle recommandé (opt-in --with-model) via LEUR gestionnaire.
    if plan.with_model:
        console.info(f"Téléchargement du modèle {AUDIOCPP_DEFAULT_MODEL_PACKAGE} (~3,9 Go)…")
        try:
            runner([str(venv_dir / "bin" / "python"), str(src / "tools" / "model_manager.py"),
                    "install", AUDIOCPP_DEFAULT_MODEL_PACKAGE], cwd=str(src))
        except Exception as exc:  # noqa: BLE001
            raise AudiocppPhaseError(f"téléchargement du modèle échoué : {exc}") from exc

    (home / "COMMIT").write_text(plan.commit + "\n", encoding="utf-8")
    if not audiocpp_is_complete(home, plan.commit):
        raise AudiocppPhaseError(f"provisionnement incomplet après build : {home}")
    console.ok(
        f"audio.cpp prêt : {home} (commit {plan.commit[:12]}) — déclarer le moteur "
        "qwen3asr dans resource_node.engines + inference.stt.backends (cf. config.example.yaml)"
    )


def _emit_config_main() -> int:
    """Point d'entrée du lanceur bash : émet la config JSON serveur sur stdout."""
    import argparse

    parser = argparse.ArgumentParser(description="Émet la config JSON audiocpp_server.")
    parser.add_argument("--emit-config", action="store_true", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--model-id", default="qwen3-asr-1.7b")
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    print(json.dumps(audiocpp_server_config(
        port=args.port, model_id=args.model_id, model_path=args.model_path, host=args.host,
    ), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_emit_config_main())

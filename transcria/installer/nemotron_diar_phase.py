"""Phase installeur : diarisation Nemotron 3 prête à l'emploi (binaire + poids).

Deux pièces, toutes deux NON gated et sans chaîne de compilation :

- le binaire ``audiocpp_cli`` — PRÉCOMPILÉ CPU de l'amont (``audiocpp_phase.
  install_prebuilt_audiocpp``, sha256 vérifié), sauf si un build CUDA de la phase
  ``audiocpp`` existe déjà (préféré : il sert aussi les runtimes STT) ;
- le GGUF ``nemotron-3-diarization-bf16.gguf`` (~199 Mo), posé sous ``<models>/`` au même
  endroit que la page « Modèles » (catalogue ``nemotron_diar``).

C'est ce que ``install.sh`` fait PAR DÉFAUT depuis 0.4.7 (diarisation ``auto`` des
configurations neuves) et ce que la page « Modèles » propose aux installations
existantes. Même patron que les autres phases : plan figé, E/S injectées, idempotent,
erreurs typées — ``python -m transcria.installer.cli nemotron-diar``.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from transcria.installer.audiocpp_phase import (
    AudiocppPhaseError,
    audiocpp_home,
    install_prebuilt_audiocpp,
)

# Mêmes valeurs que transcria/stt/nemotron_diarizer.py (ce module reste stdlib-pur :
# la tête de l'installeur tourne avant le venv — vérifié par tests/test_nemotron_diar_phase.py).
NEMOTRON_DIAR_REPO = "audio-cpp/Nemotron-3-Diarization-GGUF"
NEMOTRON_DIAR_MODEL_FILE = "nemotron-3-diarization-bf16.gguf"
NEMOTRON_DIAR_MODEL_SUBDIR = "nemotron-3-diarization"

DownloadFn = Callable[[str, str, Path], Path]


class NemotronDiarPhaseError(RuntimeError):
    """Échec de mise en place de la diarisation Nemotron 3."""


@dataclass(frozen=True)
class NemotronDiarPlan:
    runtimes_dir: Path
    models_dir: Path
    with_model: bool = True
    force: bool = False


ProbeFn = Callable[[Path], bool]


def _cli_loads(cli: Path) -> bool:
    """``audiocpp_cli --help`` sort-il en 0 ? Un build dont une lib CUDA manque échoue ici
    (code 127, « error while loading shared libraries ») — mieux vaut le voir à l'installation
    qu'à la première diarisation (vécu à la gate d'installation 2026-10-10)."""
    import subprocess

    try:
        return subprocess.run([str(cli), "--help"], capture_output=True, timeout=30, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def cuda_cli(runtimes_dir: Path, *, probe: ProbeFn = _cli_loads, console=None) -> Path | None:
    """Le ``audiocpp_cli`` du build CUDA, s'il existe ET se charge (préféré au précompilé)."""
    cli = audiocpp_home(runtimes_dir) / "bin" / "audiocpp_cli"
    if not (cli.is_file() and os.access(cli, os.X_OK)):
        return None
    if not probe(cli):
        if console is not None:
            console.warn(f"build CUDA présent mais il ne se charge pas ({cli}) — précompilé CPU posé à côté")
        return None
    return cli


def model_path(models_dir: Path) -> Path:
    return models_dir / NEMOTRON_DIAR_MODEL_SUBDIR / NEMOTRON_DIAR_MODEL_FILE


def _hf_download(repo: str, filename: str, local_dir: Path) -> Path:
    # Différé : huggingface_hub n'est là qu'après `pip install -r requirements.txt`, et
    # cette phase tourne dans le venv — la tête de l'installeur reste stdlib-pure.
    from huggingface_hub import hf_hub_download

    return Path(hf_hub_download(repo_id=repo, filename=filename, local_dir=str(local_dir)))


def apply_nemotron_diar(
    plan: NemotronDiarPlan,
    *,
    console,
    install_prebuilt: Callable[..., Path] = install_prebuilt_audiocpp,
    download: DownloadFn = _hf_download,
    probe: ProbeFn = _cli_loads,
) -> tuple[Path, Path | None]:
    """Met en place binaire + poids. Retourne ``(binaire, gguf | None)``."""
    runtimes_dir = plan.runtimes_dir.expanduser().resolve()
    models_dir = plan.models_dir.expanduser().resolve()

    cli = cuda_cli(runtimes_dir, probe=probe, console=console)
    if cli is not None and not plan.force:
        console.ok(f"audiocpp_cli du build CUDA présent : {cli} (précompilé non nécessaire)")
    else:
        try:
            cli = install_prebuilt(runtimes_dir=runtimes_dir, console=console, force=plan.force)
        except AudiocppPhaseError as exc:
            raise NemotronDiarPhaseError(str(exc)) from exc

    gguf: Path | None = None
    if plan.with_model:
        gguf = model_path(models_dir)
        if gguf.is_file() and not plan.force:
            console.ok(f"poids Nemotron 3 présents : {gguf}")
        else:
            console.info(f"Téléchargement de {NEMOTRON_DIAR_MODEL_FILE} (~199 Mo, non gated)…")
            try:
                gguf = download(NEMOTRON_DIAR_REPO, NEMOTRON_DIAR_MODEL_FILE, gguf.parent)
            except Exception as exc:  # noqa: BLE001 — remonté typé, lisible par l'installeur
                raise NemotronDiarPhaseError(f"téléchargement du GGUF échoué : {exc}") from exc
            if not gguf.is_file():
                raise NemotronDiarPhaseError(f"GGUF absent après téléchargement : {gguf}")
            console.ok(f"poids Nemotron 3 prêts : {gguf}")

    where = "build CUDA" if cli.parent == audiocpp_home(runtimes_dir) / "bin" else "précompilé CPU"
    console.ok(f"Diarisation Nemotron 3 prête ({where}) — retenue par models.diarization_backend: auto")
    return cli, gguf

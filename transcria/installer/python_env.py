"""Phase « environnement Python » de l'installateur (venv + PyTorch + dépendances).

Première tranche d'orchestration migrée de `install.sh` (SECTION 2-4) vers
l'installateur Python. Comportement **préservé à l'identique** : le venv est créé
avec l'interpréteur système (jamais re-pointé vers le venv dans `install.sh`), la
détection de PyTorch s'appuie donc sur ce même interpréteur (via
`transcria.installer.torch_env.build_install_plan`), et les commandes `pip` ciblent
explicitement le `python` du venv (équivalent du `pip` activé côté shell).

Le runner de sous-processus est injectable pour des tests sans réseau ni venv réel.
`--skip-deps` (couche build Docker / venv existant) n'installe rien : il exige
seulement un venv déjà présent.
"""
from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from transcria.installer.messages import t
from transcria.installer.torch_env import build_install_plan

Runner = Callable[..., Any]


class _ConsoleLike(Protocol):
    def info(self, message: str) -> None: ...
    def ok(self, message: str) -> None: ...
    def warn(self, message: str) -> None: ...
    def error(self, message: str) -> None: ...


@dataclass(frozen=True)
class PythonEnvPlan:
    venv_path: Path
    requirements_path: Path
    skip_deps: bool = False
    install_torch: bool = True
    cuda_version: str | None = None
    forced_cuda_tag: str | None = None


@dataclass
class PythonEnvResult:
    """Trace des décisions/actions — pour assertions de test et journalisation."""

    actions: list[str] = field(default_factory=list)

    def record(self, action: str) -> None:
        self.actions.append(action)


class PythonEnvError(RuntimeError):
    """Échec actionnable de la phase environnement Python."""


def _venv_python(venv_path: Path) -> Path:
    return venv_path / "bin" / "python"


def _run(runner: Runner, cmd: Sequence[str]) -> None:
    runner(list(cmd), check=True)


def apply_python_env(
    plan: PythonEnvPlan,
    *,
    console: _ConsoleLike,
    runner: Runner = subprocess.run,
    system_python: str | None = None,
    torch_detector: Callable[[], str] | None = None,
) -> PythonEnvResult:
    """Provisionne le venv et les dépendances selon `plan`.

    Args:
        console: rendu des messages (style `install.sh`).
        runner: exécuteur de sous-processus (signature `subprocess.run`), injectable.
        system_python: interpréteur pour créer le venv (défaut: l'interpréteur courant,
            qui est l'interpréteur système car `install.sh` invoque ce CLI avant
            l'activation du venv).
        torch_detector: détecteur de PyTorch installé (défaut: celui de
            `build_install_plan`, qui sonde l'interpréteur courant — comportement
            historique préservé).
    """
    result = PythonEnvResult()
    venv_python = _venv_python(plan.venv_path)
    system_python = system_python or sys.executable

    if plan.skip_deps:
        if not venv_python.exists():
            raise PythonEnvError(
                f"--skip-deps requiert un environnement Python déjà présent dans {plan.venv_path} "
                "(venv existant ou couche build Docker)"
            )
        console.info(t("pe_skip_deps", venv=plan.venv_path))
        result.record("skip-deps")
        return result

    # ── venv ──────────────────────────────────────────────────────────────
    if (plan.venv_path / "bin" / "activate").exists():
        console.ok(t("pe_venv_existing", venv=plan.venv_path))
        result.record("venv-existing")
    else:
        console.info(t("pe_venv_create"))
        _run(runner, [system_python, "-m", "venv", str(plan.venv_path)])
        console.ok(t("pe_venv_created", venv=plan.venv_path))
        result.record("venv-created")

    console.info(t("pe_pip_upgrade"))
    _run(runner, [str(venv_python), "-m", "pip", "install", "--upgrade", "pip", "--quiet"])
    result.record("pip-upgrade")

    # ── PyTorch ───────────────────────────────────────────────────────────
    torch_plan = build_install_plan(
        install_torch=plan.install_torch,
        cuda_version=plan.cuda_version,
        forced_tag=plan.forced_cuda_tag,
        installed_detector=torch_detector,
    )
    if torch_plan.cuda_warning:
        console.warn(torch_plan.cuda_warning)

    if torch_plan.action == "skip":
        console.info(t("pe_torch_skip"))
    elif torch_plan.action == "already-installed":
        console.ok(t("pe_torch_present", cuda=torch_plan.installed_cuda))
    elif torch_plan.action == "install-cpu":
        console.info(t("pe_torch_cpu_start"))
        # torchcodec installé ICI, depuis le même index que torch : c'est le décodeur audio
        # de pyannote.audio 4.x, couplé à l'ABI/CUDA de torch. Le laisser arriver en transitif
        # via PyPI tirerait un wheel bâti pour un autre torch → AudioDecoder cassé.
        _run(runner, [str(venv_python), "-m", "pip", "install", "torch", "torchvision", "torchaudio", "torchcodec", "--quiet"])
        console.ok(t("pe_torch_installed"))
    elif torch_plan.action == "install-cuda":
        console.info(t("pe_torch_cuda_start", tag=torch_plan.cuda_tag))
        _run(runner, [
            str(venv_python), "-m", "pip", "install", "torch", "torchvision", "torchaudio", "torchcodec",
            "--index-url", f"https://download.pytorch.org/whl/{torch_plan.cuda_tag}", "--quiet",
        ])
        console.ok(t("pe_torch_installed"))
    else:  # pragma: no cover - garde défensive (build_install_plan est exhaustif)
        raise PythonEnvError(f"Action PyTorch inconnue : {torch_plan.action}")
    result.record(f"torch-{torch_plan.action}")

    # ── requirements ────────────────────────────────────────────────────────
    console.info(t("pe_deps_start"))
    _run(runner, [str(venv_python), "-m", "pip", "install", "-r", str(plan.requirements_path), "--quiet"])
    console.ok(t("pe_deps_ok"))
    result.record("requirements")

    return result

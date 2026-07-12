"""Phase installeur : runtime STT servi parakeet.cpp (backend `nemotron`).

Patron rétréci d'``audiocpp_phase`` : parakeet.cpp (mudler, MIT) n'a NI venv
outils NI config JSON — un binaire ``parakeet-server`` + un GGUF suffisent.
Provisionne ``<runtimes_dir>/parakeetcpp/{src,bin,COMMIT}`` ; le GGUF Nemotron
passe par le catalogue de modèles (kind ``gguf`` → ``models/parakeet-cpp/``,
page « Modèles » ou ``hf download``).

Spike santé consigné (2026-07-12, commit épinglé) : PAS de /v1/models (404),
``/health`` → 200 {"status":"ok"} ; champ multipart ``language`` toléré.
⇒ manifeste : ``health_path: /health`` (http_2xx standard).
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from transcria.installer.audiocpp_phase import resolve_runtimes_dir  # même racine runtimes/

# Commit épinglé = l'état QUALIFIÉ sur notre benchmark (Nemotron 0,492 WER 8/8,
# 3 requêtes consécutives identiques — pas de bug de session).
PARAKEETCPP_REPO = "https://github.com/mudler/parakeet.cpp"
# SHA COMPLET requis : `git fetch origin <sha>` refuse les SHA courts (exit 128).
PARAKEETCPP_PINNED_COMMIT = "e8acc6172a94e20a952cf1843decace5d771a94b"


class Runner(Protocol):
    def __call__(self, cmd: list[str], *, cwd: str | None = None) -> None: ...


class ParakeetcppPhaseError(RuntimeError):
    """Échec de provisionnement du runtime parakeet.cpp."""


@dataclass(frozen=True)
class ParakeetcppPlan:
    runtimes_dir: Path
    commit: str = PARAKEETCPP_PINNED_COMMIT
    force: bool = False
    jobs: int = 0  # 0 = nproc
    # Même piège que audio.cpp : sans réglage, cmake peut retomber sur une arch
    # CUDA inadaptée → kernels ggml qui SIGABRT à l'inférence. « native » = le GPU
    # de CETTE machine (installeur local).
    cuda_archs: str = "native"


def parakeetcpp_home(runtimes_dir: Path) -> Path:
    return runtimes_dir / "parakeetcpp"


def parakeetcpp_is_complete(home: Path, commit: str) -> bool:
    binary = home / "bin" / "parakeet-server"
    marker = home / "COMMIT"
    return (
        binary.is_file()
        and os.access(binary, os.X_OK)
        and marker.is_file()
        and marker.read_text(encoding="utf-8").strip() == commit
    )


def apply_parakeetcpp(plan: ParakeetcppPlan, *, console, runner: Runner) -> None:
    home = parakeetcpp_home(plan.runtimes_dir.expanduser())
    if not plan.force and parakeetcpp_is_complete(home, plan.commit):
        console.ok(f"parakeet.cpp déjà provisionné : {home} (commit {plan.commit[:12]} — --force pour reconstruire)")
        return

    src = home / "src"
    bin_dir = home / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    if plan.force and src.exists():
        shutil.rmtree(src)
    if not (src / ".git").exists():
        console.info(f"Clone parakeet.cpp → {src} (submodules ggml)")
        try:
            runner(["git", "clone", "--recursive", PARAKEETCPP_REPO, str(src)])
        except Exception as exc:  # noqa: BLE001
            raise ParakeetcppPhaseError(f"clone parakeet.cpp échoué : {exc}") from exc
    try:
        runner(["git", "-C", str(src), "fetch", "origin", plan.commit])
        runner(["git", "-C", str(src), "checkout", plan.commit])
        runner(["git", "-C", str(src), "submodule", "update", "--init", "--recursive"])
    except Exception as exc:  # noqa: BLE001
        raise ParakeetcppPhaseError(f"checkout du commit épinglé {plan.commit[:12]} échoué : {exc}") from exc

    jobs = plan.jobs or (os.cpu_count() or 4)
    console.info(f"Compilation parakeet-server (CUDA, -j{jobs}) — plusieurs minutes…")
    try:
        runner(["cmake", "-S", str(src), "-B", str(src / "build"),
                "-DCMAKE_BUILD_TYPE=Release", "-DPARAKEET_GGML_CUDA=ON",
                f"-DCMAKE_CUDA_ARCHITECTURES={plan.cuda_archs}"])
        runner(["cmake", "--build", str(src / "build"), "-j", str(jobs)])
    except Exception as exc:  # noqa: BLE001
        raise ParakeetcppPhaseError(f"compilation parakeet.cpp échouée : {exc}") from exc
    built = src / "build" / "examples" / "server" / "parakeet-server"
    if not built.is_file():
        raise ParakeetcppPhaseError(f"binaire absent après compilation : {built}")
    shutil.copy2(built, bin_dir / "parakeet-server")

    (home / "COMMIT").write_text(plan.commit + "\n", encoding="utf-8")
    if not parakeetcpp_is_complete(home, plan.commit):
        raise ParakeetcppPhaseError(f"provisionnement incomplet après build : {home}")
    console.ok(
        f"parakeet.cpp prêt : {home} (commit {plan.commit[:12]}) — GGUF Nemotron via la page "
        "« Modèles » puis déclarer le moteur nemotron (cf. config.example.yaml)"
    )

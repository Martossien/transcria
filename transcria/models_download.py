"""Téléchargement des modèles avec suivi par fichier de statut (lot 2/3).

Le worker web NE bloque JAMAIS : le téléchargement (potentiellement des dizaines de Go) tourne
en **sous-process détaché** (la CLI ``maintenance model-download``). La **progression** se lit par
polling d'un fichier de statut JSON + la **taille réellement sur disque** de la cible rapportée au
total du repo (HF) — pas de hook tqdm fragile. Gated → token HF passé par l'ENV (jamais en argv).

Tout est injectable (``hf_download``/``popen``) pour tester sans réseau ni sous-process réel.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from transcria.models_catalog import ModelSpec, disk_free_bytes, resolve_hf_home, resolve_models_dir, resolve_runtimes_dir

STATUS_DIRNAME = ".downloads"


def status_path(models_dir: Path, role: str) -> Path:
    return models_dir / STATUS_DIRNAME / f"{role}.json"


def target_dir_for(spec: ModelSpec, *, hf_home: Path, models_dir: Path) -> Path:
    """Dossier qui GROSSIT pendant le téléchargement (base du calcul de progression)."""
    if spec.kind == "gguf":
        return models_dir / spec.target_subdir
    if spec.kind == "runtime":
        return resolve_runtimes_dir() / spec.target_subdir
    return hf_home / "hub" / ("models--" + spec.repo_id.replace("/", "--"))


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def _write_status(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def check_space(spec: ModelSpec, *, hf_home: Path, models_dir: Path, margin: float = 1.15) -> tuple[bool, str]:
    """Assez de place pour ~``est_gb`` × marge ? Retourne (ok, message)."""
    target = target_dir_for(spec, hf_home=hf_home, models_dir=models_dir)
    needed = spec.est_gb * margin * 1e9
    free = disk_free_bytes(target)
    if free >= needed:
        return True, f"{round(free / 1e9, 1)} Go libres pour ~{spec.est_gb} Go requis"
    return False, (f"espace insuffisant : {round(free / 1e9, 1)} Go libres, "
                   f"~{round(spec.est_gb * margin, 1)} Go requis")


def _repo_total_bytes(spec: ModelSpec, token: str | None) -> int:
    from huggingface_hub import HfApi

    info = HfApi().repo_info(spec.repo_id, files_metadata=True, token=token or None)
    return sum((s.size or 0) for s in (info.siblings or [])
               if spec.file is None or s.rfilename == spec.file)


def _configure_hf_transfer() -> bool:
    """Active le téléchargement multi-flux Rust (hf_transfer) si le paquet est présent.

    À appeler AVANT tout import de huggingface_hub : la constante
    ``HF_HUB_ENABLE_HF_TRANSFER`` y est figée à l'import. Kill-switch :
    ``TRANSCRIA_NO_HF_TRANSFER=1`` (proxies capricieux, débogage).
    """
    if os.environ.get("TRANSCRIA_NO_HF_TRANSFER"):
        return False
    try:
        import hf_transfer  # noqa: F401
    except ImportError:
        return False
    os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "1"
    return True


def _disable_hf_transfer_runtime() -> None:
    """Coupe hf_transfer APRÈS import de huggingface_hub (repli sur la voie classique)."""
    os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "0"
    try:
        from huggingface_hub import constants

        constants.HF_HUB_ENABLE_HF_TRANSFER = False
    except Exception:  # noqa: BLE001 — repli best-effort, le retry échouera de lui-même sinon
        pass


def _fetch_with_hf_transfer_fallback(fetch: Callable[[], None], hf_fast: bool) -> None:
    """Un essai en voie rapide, puis repli UNE fois en voie classique.

    hf_transfer est moins tolérant que la voie Python (proxies, reprises
    partielles) : son échec ne doit jamais coûter un téléchargement qui aurait
    réussi sans lui.
    """
    if not hf_fast:
        fetch()
        return
    try:
        fetch()
    except Exception:  # noqa: BLE001 — tout échec de la voie rapide déclenche le repli
        _disable_hf_transfer_runtime()
        fetch()


def run_download(
    spec: ModelSpec,
    *,
    hf_home: Path,
    models_dir: Path,
    token: str | None,
    status_file: Path,
    hf_download: Callable[[ModelSpec, Path, Path, str | None], None] | None = None,
    total_fn: Callable[[ModelSpec, str | None], int] | None = None,
) -> dict:
    """Effectue le téléchargement BLOQUANT (dans le sous-process) en publiant le statut."""
    started = datetime.now(timezone.utc).isoformat()
    hf_fast = _configure_hf_transfer()

    def _base(**extra) -> dict:
        # kind/subdir/file rendent le statut AUTO-SUFFISANT : l'endpoint de progression calcule
        # la cible sans reconstruire le catalogue (donc sans détection GPU) à chaque poll.
        return {"role": spec.role, "repo": spec.repo_id, "kind": spec.kind,
                "subdir": spec.target_subdir, "file": spec.file, "started_at": started, **extra}

    try:
        total = (total_fn or _repo_total_bytes)(spec, token)
    except Exception:  # noqa: BLE001 — total indisponible ⇒ progression indéterminée, pas d'échec
        total = 0
    _write_status(status_file, _base(status="downloading", total_bytes=total))

    try:
        if hf_download is not None:
            hf_download(spec, hf_home, models_dir, token)
        elif spec.kind == "runtime":
            # Poids gérés par le runtime servi (audio.cpp) : téléchargement DÉLÉGUÉ à
            # SON model_manager (venv dédié, provisionnés par `installer.cli audiocpp`).
            # spec.file porte l'id du paquet chez eux (cf. _SERVED_STT_SOURCES).

            home = resolve_runtimes_dir() / "audiocpp"
            manager = home / "src" / "tools" / "model_manager.py"
            py = home / "venv" / "bin" / "python"
            if not (py.exists() and manager.exists()):
                raise RuntimeError(
                    "runtime audio.cpp non provisionné — lancer d'abord : "
                    "venv/bin/python -m transcria.installer.cli audiocpp"
                )
            if not spec.file:
                raise ValueError("modèle runtime sans id de paquet")
            subprocess.run([str(py), str(manager), "install", spec.file],
                           check=True, cwd=str(home / "src"))
        elif spec.kind == "gguf":
            if not spec.file:
                raise ValueError("modèle GGUF sans nom de fichier")
            gguf_file: str = spec.file  # figé après la garde (mypy ne narrowe pas dans la closure)

            def _fetch() -> None:
                from huggingface_hub import hf_hub_download

                hf_hub_download(repo_id=spec.repo_id, filename=gguf_file,
                                local_dir=str(models_dir / spec.target_subdir), token=token or None)

            _fetch_with_hf_transfer_fallback(_fetch, hf_fast)
        else:
            def _fetch() -> None:
                from huggingface_hub import snapshot_download

                snapshot_download(repo_id=spec.repo_id, token=token or None)  # → cache HF_HOME

            _fetch_with_hf_transfer_fallback(_fetch, hf_fast)
    except Exception as exc:  # noqa: BLE001 — on RAPPORTE l'échec dans le statut (repris par l'UI)
        _write_status(status_file, _base(status="error", total_bytes=total, message=str(exc)[:400]))
        return {"ok": False, "error": str(exc)}

    _write_status(status_file, _base(status="done", total_bytes=total,
                                     finished_at=datetime.now(timezone.utc).isoformat()))
    return {"ok": True}


def _target_from_status(data: dict, hf_home: Path, models_dir: Path) -> Path:
    if data.get("kind") == "gguf":
        return models_dir / (data.get("subdir") or "")
    return hf_home / "hub" / ("models--" + str(data.get("repo", "")).replace("/", "--"))


def progress_by_role(role: str, *, hf_home: Path, models_dir: Path) -> dict:
    """Progression SANS catalogue ni détection GPU : lit le statut auto-suffisant + du(cible).
    Pensé pour l'endpoint polled toutes les ~2 s."""
    path = status_path(models_dir, role)
    if not path.exists():
        return {"status": "absent"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "absent"}
    total = int(data.get("total_bytes") or 0)
    downloaded = _dir_size(_target_from_status(data, hf_home, models_dir))
    data["downloaded_bytes"] = downloaded
    data["pct"] = min(round(100 * downloaded / total), 100) if total else None
    return data


def read_progress(spec: ModelSpec, *, hf_home: Path, models_dir: Path) -> dict:
    """Statut courant + progression calculée par taille sur disque / total du repo."""
    path = status_path(models_dir, spec.role)
    if not path.exists():
        return {"status": "absent"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "absent"}
    total = int(data.get("total_bytes") or 0)
    downloaded = _dir_size(target_dir_for(spec, hf_home=hf_home, models_dir=models_dir))
    data["downloaded_bytes"] = downloaded
    data["pct"] = min(round(100 * downloaded / total), 100) if total else None
    return data


def start_download(
    spec: ModelSpec,
    *,
    token: str | None = None,
    popen: Callable[..., object] = subprocess.Popen,
) -> Path:
    """Lance la CLI ``model-download`` en sous-process DÉTACHÉ. Retourne le fichier de statut.

    Le token passe par l'ENV ``HF_TOKEN`` (jamais en argv → pas de fuite dans ``ps``)."""
    models_dir = resolve_models_dir()
    status_file = status_path(models_dir, spec.role)
    _write_status(status_file, {"role": spec.role, "repo": spec.repo_id, "kind": spec.kind,
                                "subdir": spec.target_subdir, "file": spec.file, "status": "starting"})

    cmd = [sys.executable, "-m", "transcria.maintenance.cli", "model-download",
           "--role", spec.role, "--repo", spec.repo_id, "--kind", spec.kind]
    if spec.file:
        cmd += ["--file", spec.file]
    if spec.target_subdir:
        cmd += ["--subdir", spec.target_subdir]

    env = dict(os.environ)
    if token:
        env["HF_TOKEN"] = token
    log = status_file.with_suffix(".log")
    with open(log, "wb") as log_file:
        popen(cmd, stdout=log_file, stderr=log_file, start_new_session=True, env=env, cwd=os.getcwd())
    return status_file


def download_from_args(*, role: str, repo: str, kind: str, file: str | None, subdir: str) -> int:
    """Point d'entrée du sous-process (CLI) : reconstruit un ModelSpec minimal et télécharge."""
    spec = ModelSpec(role=role, label=role, repo_id=repo, file=file, kind=kind,
                     target_subdir=subdir, gated=False, license="", license_url="", est_gb=0.0)
    hf_home, models_dir = resolve_hf_home(), resolve_models_dir()
    result = run_download(spec, hf_home=hf_home, models_dir=models_dir,
                          token=os.environ.get("HF_TOKEN") or None,
                          status_file=status_path(models_dir, role))
    return 0 if result.get("ok") else 1

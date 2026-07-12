"""Phase installeur : site-packages isolé Transformers 5 pour le backend `moss`.

Le backend MOSS-Transcribe-Diarize exige Transformers 5.x alors que le venv
projet reste en 4.x : l'inférence tourne dans un worker subprocess dont le
PYTHONPATH pointe d'abord vers ce site (cf. ``transcria/stt/moss_transcriber.py``).
Cette phase le crée par ``pip install --target`` — AUCUN torch dans le site
(celui du venv est réutilisé), ~800 Mo.

Opt-in : ni ``install.sh`` ni les images ne l'exécutent par défaut ; c'est
``python -m transcria.installer.cli moss-site`` (opérateur) ou le build de
l'image bundled qui l'appellent. Idempotent : site déjà complet ⇒ no-op.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

# Paquets installés dans le site isolé. transformers 5 tire ses propres deps
# (tokenizers, huggingface-hub >= 1.x…) qui MASQUENT celles du venv pour le
# worker uniquement — c'est le but.
MOSS_SITE_SPEC = (
    "transformers>=5.0,<6.0",
    "git+https://github.com/OpenMOSS/MOSS-Transcribe-Diarize.git",
)
# Marqueurs d'un site complet (répertoires importables).
_REQUIRED_MARKERS = ("transformers", "moss_transcribe_diarize")


class Runner(Protocol):
    def __call__(self, cmd: list[str]) -> None: ...


class MossSiteError(RuntimeError):
    """Échec de provisionnement du site isolé."""


@dataclass(frozen=True)
class MossSitePlan:
    site_dir: Path
    python_bin: Path
    force: bool = False


def site_is_complete(site_dir: Path) -> bool:
    return all((site_dir / marker).is_dir() for marker in _REQUIRED_MARKERS)


def apply_moss_site(plan: MossSitePlan, *, console, runner: Runner) -> None:
    site_dir = plan.site_dir.expanduser()
    if not plan.force and site_is_complete(site_dir):
        console.ok(f"Site moss déjà provisionné : {site_dir} (utiliser --force pour réinstaller)")
        return
    if not plan.python_bin.exists():
        raise MossSiteError(f"python introuvable : {plan.python_bin}")
    site_dir.mkdir(parents=True, exist_ok=True)
    console.info(
        f"Installation du site Transformers 5 isolé dans {site_dir} "
        "(~800 Mo, sans torch — celui du venv est réutilisé)…"
    )
    cmd = [str(plan.python_bin), "-m", "pip", "install", "--target", str(site_dir), "--quiet"]
    if plan.force:
        cmd.append("--upgrade")
    cmd.extend(MOSS_SITE_SPEC)
    try:
        runner(cmd)
    except Exception as exc:  # noqa: BLE001 — remonté en erreur de phase typée
        raise MossSiteError(f"pip install --target a échoué : {exc}") from exc
    if not site_is_complete(site_dir):
        raise MossSiteError(
            f"site incomplet après installation ({site_dir}) : "
            f"marqueurs attendus {_REQUIRED_MARKERS}"
        )
    console.ok(
        f"Site moss prêt : {site_dir} — pointer moss.moss_site dessus dans config.yaml "
        "puis models.stt_backend: moss"
    )

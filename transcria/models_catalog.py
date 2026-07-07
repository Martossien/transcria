"""Catalogue des modèles requis par l'install (LLM d'arbitrage, STT, diarisation).

Piloté par la config : on ne liste que ce dont CETTE installation a besoin (backend STT/diar
configuré + palier LLM recommandé pour le VRAM). Sert la page « Modèles » : statut présent/absent,
taille sur disque, caractère *gated* (token HF + licence), estimation de taille, place disque.

Pur et sans réseau (le téléchargement vit ailleurs). Réutilise les primitives de
``install_models`` (cache HF) et ``install_arbitrage`` (palier GGUF).
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from transcria.install_models import COHERE_MODEL_ID, PYANNOTE_MODEL_ID, find_hf_cache_model

# Backends STT non hardcodés côté runtime, mais leur SOURCE HF l'est ici (pour le téléchargement).
_STT_SOURCES: dict[str, dict] = {
    "cohere": {"repo": COHERE_MODEL_ID, "gated": True, "license": "Cohere (accès repo requis)",
               "license_url": "https://huggingface.co/" + COHERE_MODEL_ID, "est_gb": 6.0},
    "whisper": {"repo": "openai/whisper-large-v3", "gated": False, "license": "MIT",
                "license_url": "https://huggingface.co/openai/whisper-large-v3", "est_gb": 3.1},
}
_DIAR_SOURCES: dict[str, dict] = {
    "pyannote": {"repo": PYANNOTE_MODEL_ID, "gated": True,
                 "license": "pyannote (token HF + acceptation des conditions)",
                 "license_url": "https://huggingface.co/" + PYANNOTE_MODEL_ID, "est_gb": 0.1},
    "sortformer": {"repo": "nvidia/diar_streaming_sortformer_4spk-v2.1", "gated": False,
                   "license": "NVIDIA Open Model License",
                   "license_url": "https://huggingface.co/nvidia/diar_streaming_sortformer_4spk-v2.1",
                   "est_gb": 0.6},
}


@dataclass(frozen=True)
class ModelSpec:
    role: str            # arbitrage_llm | stt | diarization
    label: str
    repo_id: str
    file: str | None     # fichier unique (GGUF) ou None = snapshot complet
    kind: str            # gguf (→ models_dir) | hf_cache (→ HF_HOME)
    target_subdir: str   # gguf : sous-dossier de models_dir
    gated: bool
    license: str
    license_url: str
    est_gb: float
    tier: str = ""   # LLM d'arbitrage uniquement : palier VRAM (ex. "64") → profil de bascule


def resolve_hf_home() -> Path:
    return Path(os.environ.get("HF_HOME") or (Path.home() / ".cache" / "huggingface"))


def resolve_models_dir() -> Path:
    return Path(os.environ.get("MODELS_DIR") or "./models")


def build_catalog(cfg: dict, *, total_vram_mb: int | None = None) -> list[ModelSpec]:
    """Modèles nécessaires à CETTE install (STT + diarisation configurés + palier LLM VRAM)."""
    models = cfg.get("models", {}) or {}
    specs: list[ModelSpec] = []

    # LLM d'arbitrage : palier GGUF recommandé pour le VRAM (best-effort).
    if total_vram_mb:
        try:
            from transcria.install_arbitrage import get_tier_metadata, recommend_tier

            tier = recommend_tier(total_vram_mb)
            meta = get_tier_metadata(tier)
            specs.append(ModelSpec(
                role="arbitrage_llm", label=f"LLM d'arbitrage ({meta.file})",
                repo_id=meta.repo, file=meta.file, kind="gguf", target_subdir=meta.directory,
                gated=False, license="Apache-2.0 / MIT (quantifications unsloth)",
                license_url="https://huggingface.co/" + meta.repo, est_gb=_gguf_est_gb(meta.file),
                tier=tier,
            ))
        except Exception:  # noqa: BLE001 — pas de palier résoluble ⇒ on n'ajoute pas la ligne LLM
            pass

    stt = _STT_SOURCES.get(str(models.get("stt_backend") or "cohere"))
    if stt:
        specs.append(ModelSpec(
            role="stt", label=f"STT — {models.get('stt_backend')}", repo_id=stt["repo"],
            file=None, kind="hf_cache", target_subdir="", gated=stt["gated"],
            license=stt["license"], license_url=stt["license_url"], est_gb=stt["est_gb"]))

    diar = _DIAR_SOURCES.get(str(models.get("diarization_backend") or "pyannote"))
    if diar:
        specs.append(ModelSpec(
            role="diarization", label=f"Diarisation — {models.get('diarization_backend')}",
            repo_id=diar["repo"], file=None, kind="hf_cache", target_subdir="", gated=diar["gated"],
            license=diar["license"], license_url=diar["license_url"], est_gb=diar["est_gb"]))
    return specs


def _gguf_est_gb(filename: str) -> float:
    """Estimation grossière de taille GGUF depuis la quantification du nom (pour le check espace)."""
    name = filename.lower()
    for token, gb in (("q8", 38.0), ("q6", 29.0), ("q5", 25.0), ("iq4", 20.0), ("q4", 20.0)):
        if token in name:
            return gb
    return 20.0


def served_llm_gguf(cfg: dict) -> Path | None:
    """Chemin GGUF RÉELLEMENT servi = ``--model`` du script de lancement d'arbitrage.

    C'est la source la plus fiable pour la LLM : le programme charge exactement ce chemin.
    Gère la valeur littérale (script déployé) et le templating ``${MODELS_DIR:-…}`` (wrapper généré)."""
    script = (cfg.get("services", {}) or {}).get("arbitrage_script") or "./scripts/launch_arbitrage.sh"
    path = Path(script) if Path(script).is_absolute() else Path.cwd() / script
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    match = re.search(r'--model\s+"?([^"\s\\]+\.gguf)', text)
    if not match:
        return None
    raw = re.sub(r"\$\{MODELS_DIR(?::-[^}]*)?\}", str(resolve_models_dir()), match.group(1))
    return Path(raw)


def _candidate_hf_hubs(hf_home: Path) -> list[Path]:
    """Répertoires ``hub/`` de cache HF à sonder (là où transformers/HF chargent les modèles)."""
    hubs, seen = [], set()
    for base in (hf_home, Path.home() / ".cache" / "huggingface", Path("/root/.cache/huggingface")):
        hub = Path(base) / "hub"
        if str(hub) not in seen:
            seen.add(str(hub))
            hubs.append(hub)
    transformers_cache = os.environ.get("TRANSFORMERS_CACHE")
    if transformers_cache and str(Path(transformers_cache)) not in seen:
        hubs.append(Path(transformers_cache))  # peut déjà pointer un hub
    return hubs


def _candidate_model_roots(models_dir: Path, extra: tuple[Path, ...]) -> list[Path]:
    """Racines où un GGUF peut vivre (MODELS_DIR, /root/models, ~/models, ./models, + hints)."""
    roots, seen = [], set()
    for root in (models_dir, Path("/root/models"), Path.home() / "models", Path("./models"), *extra):
        resolved = str(Path(root))
        if resolved not in seen:
            seen.add(resolved)
            roots.append(Path(root))
    return roots


def _present(path: Path) -> dict:
    return {"present": True, "path": str(path), "size_bytes": path.stat().st_size}


def model_status(
    spec: ModelSpec,
    *,
    hf_home: Path,
    models_dir: Path,
    served_path: Path | None = None,
    extra_roots: tuple[Path, ...] = (),
) -> dict:
    """Présence + taille sur disque, en cherchant à PLUSIEURS endroits (aucun réseau).

    GGUF : le ``--model`` réellement servi d'abord, puis le fichier cherché sous plusieurs racines
    (le sous-dossier peut différer de l'attendu). HF cache : plusieurs répertoires ``hub/``.
    Résilient aux dossiers non lisibles (ex. ``/root/models`` pour un process non-root)."""
    if spec.kind == "gguf" and spec.file:
        if served_path is not None and served_path.name == spec.file:
            try:
                if served_path.is_file():
                    return _present(served_path)
            except OSError:
                pass
        for root in _candidate_model_roots(models_dir, extra_roots):
            try:
                direct = root / spec.target_subdir / spec.file
                if direct.is_file():
                    return _present(direct)
                for found in root.glob(f"**/{spec.file}"):  # le fichier où qu'il soit sous la racine
                    if found.is_file():
                        return _present(found)
            except OSError:
                continue  # racine non lisible / absente → on passe à la suivante
    else:  # hf_cache
        for hub in _candidate_hf_hubs(hf_home):
            cached = find_hf_cache_model(hub, spec.repo_id)
            if cached is not None:
                return {"present": True, "path": str(cached), "size_bytes": _dir_size(cached)}
    return {"present": False, "path": None, "size_bytes": 0}


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def disk_free_bytes(path: Path) -> int:
    """Espace libre du système de fichiers contenant ``path`` (remonte au 1er parent existant)."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def catalog_with_status(cfg: dict, *, total_vram_mb: int | None = None) -> dict:
    """Vue complète pour l'UI : modèles + statut + place disque des deux cibles."""
    hf_home, models_dir = resolve_hf_home(), resolve_models_dir()
    served = served_llm_gguf(cfg)                              # GGUF réellement servi (script de lancement)
    extra_roots = (served.parent,) if served else ()          # + son dossier comme racine de recherche
    specs = build_catalog(cfg, total_vram_mb=total_vram_mb)
    items = []
    for spec in specs:
        status = model_status(spec, hf_home=hf_home, models_dir=models_dir,
                              served_path=served, extra_roots=extra_roots)
        items.append({"spec": spec, **status})
    return {
        "items": items,
        "hf_home": str(hf_home),
        "models_dir": str(models_dir),
        "hf_free_gb": round(disk_free_bytes(hf_home) / 1e9, 1),
        "models_free_gb": round(disk_free_bytes(models_dir) / 1e9, 1),
    }

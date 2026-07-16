"""Bibliothèque modèles partagée install ↔ runtime (vague C6).

Les identifiants de modèles et la détection en cache HuggingFace sont consommés
à la fois par l'installateur (``installer/models.py``) et par le RUNTIME
(``models_catalog`` pour la page Modèles, ``stt/cohere_transcriber`` pour le
chemin par défaut) : ils vivent ici, stdlib-purs, sans tirer la surface CLI.
"""
from __future__ import annotations

from pathlib import Path

PYANNOTE_MODEL_ID = "pyannote/speaker-diarization-community-1"
SQUIM_RELATIVE_PATH = Path("hub") / "torchaudio" / "models" / "squim_objective_dns2020.pth"
COHERE_MODEL_ID = "CohereLabs/cohere-transcribe-03-2026"
COHERE_DEFAULT_RELATIVE_PATH = Path("models") / "cohere-asr" / "cohere-transcribe-03-2026"


def find_hf_cache_model(hf_cache: Path, repo_id: str) -> Path | None:
    """Retourne le répertoire de cache HF d'un modèle par repo id `org/name`.

    Un `cohere_model_path` comme `CohereLabs/cohere-transcribe-03-2026` n'est PAS un chemin
    local : le modèle vit dans le cache HF (`hub/models--CohereLabs--…/snapshots/…`). On le
    détecte comme pyannote, au lieu de chercher (à tort) un répertoire local inexistant.
    """
    if not repo_id or "/" not in repo_id or repo_id.startswith((".", "/")):
        return None  # chemin local, pas un repo id
    cache_dir = Path(hf_cache) / ("models--" + repo_id.replace("/", "--"))
    snapshots = cache_dir / "snapshots"
    try:
        if cache_dir.is_dir() and snapshots.is_dir() and any(snapshots.iterdir()):
            return cache_dir
    except OSError:
        # Cache non lisible (ex. ~/.cache/huggingface d'un autre compte, /root en CI) :
        # on considère le modèle ABSENT plutôt que de propager (parité avec la branche gguf).
        return None
    return None

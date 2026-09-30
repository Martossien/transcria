"""Dossiers locaux des poids et des runtimes — source unique de leur résolution."""
from __future__ import annotations

import os
from pathlib import Path


def resolve_runtimes_dir() -> Path:
    """Runtimes servis (audio.cpp, parakeet.cpp) : ``TRANSCRIA_RUNTIMES_DIR`` sinon ``./runtimes``."""
    return Path(os.environ.get("TRANSCRIA_RUNTIMES_DIR") or "./runtimes")


def resolve_models_dir() -> Path:
    """Poids GGUF téléchargés : ``MODELS_DIR`` sinon ``./models``."""
    return Path(os.environ.get("MODELS_DIR") or "./models")

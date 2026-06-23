"""Verrou global d'instanciation de modèles (sûreté concurrence).

`accelerate.init_empty_weights()` — déclenché par `transformers.from_pretrained(device_map=…)`
ou `low_cpu_mem_usage=True` — **monkeypatch GLOBALEMENT** la création des `nn.Module` pour
placer leurs poids sur le device `meta`, à l'intérieur d'un *context manager NON thread-safe*.
Conséquence sous charge : si un thread charge un modèle `device_map` (ex. Cohere STT) pendant
qu'un autre instancie un modèle (ex. pipeline pyannote), les modules de ce dernier atterrissent
eux aussi sur `meta` → `pipeline.to(device)` lève « Cannot copy out of meta tensor; no data! ».

On sérialise donc l'**instanciation** des modèles derrière ce verrou unique de process. La
fenêtre protégée est courte (construction + `.to(device)`) ; l'**inférence** reste concurrente.
Verrou **réentrant** (un chargement peut en imbriquer un autre sans interblocage).

Tout nouveau chargement de modèle torch/transformers/NeMo doit passer par ce verrou.
"""
from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

_MODEL_LOAD_LOCK = threading.RLock()


@contextmanager
def model_load_lock() -> Iterator[None]:
    """Sérialise l'instanciation d'un modèle (cf. docstring module)."""
    with _MODEL_LOAD_LOCK:
        yield

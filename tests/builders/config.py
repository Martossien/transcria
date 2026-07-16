"""Builder de config de test — schéma-complète et déterministe."""
from copy import deepcopy

from transcria.config.loader import _deep_merge, get_default_config


def make_config(overrides: dict | None = None, *, jobs_dir: str | None = None) -> dict:
    """Config complète et DÉTERMINISTE pour un test.

    Part des défauts du loader (``get_default_config()`` — PAS de ``config.yaml``
    machine : le même test donne le même résultat partout) et fusionne
    ``overrides`` en profondeur. ``jobs_dir`` (raccourci du cas de loin le plus
    fréquent) pointe le stockage des jobs vers un répertoire de test.
    """
    cfg = deepcopy(get_default_config())
    if jobs_dir is not None:
        cfg.setdefault("storage", {})["jobs_dir"] = str(jobs_dir)
    if overrides:
        cfg = _deep_merge(cfg, overrides)
    return cfg

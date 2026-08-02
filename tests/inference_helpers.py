"""Aides des tests du service d'inférence.

Module séparé (convention `net_helpers`) : une fonction posée dans `conftest.py` ne
s'importe pas de façon fiable — un paquet `tests` installé dans le venv masque le nôtre.
"""
from __future__ import annotations


def inference_dev_config(base: dict | None = None, *, audio_root=None) -> dict:
    """Configuration du service d'inférence en mode OUVERT **ASSUMÉ** (passe sécurité S1.1).

    Depuis S1.1, `create_app` refuse de démarrer sans clé API : un service qui ne peut pas
    s'authentifier ne doit pas servir. Les tests de ROUTES n'ont aucune opinion sur
    l'authentification — ils déclarent donc explicitement le mode ouvert, au lieu d'en
    hériter par défaut. C'est exactement ce que la garde cherche à obtenir : que « ouvert »
    soit toujours une phrase écrite quelque part.

    `audio_root` déclare où vit l'audio du test. Depuis S1.1, `file_ref` est borné : sans
    allowlist ni `storage.jobs_dir`, un chemin est refusé. Les tests de routes qui posent
    leur WAV dans un `tmp_path` doivent donc dire lequel — c'est une ligne, et elle rend
    visible ce que le service accepte de lire.
    """
    cfg = dict(base or {})
    inference = dict(cfg.get("inference") or {})
    inference["auth"] = {**(inference.get("auth") or {}), "allow_unauthenticated": True}
    if audio_root is not None:
        inference["allowed_audio_roots"] = [str(audio_root)]
    cfg["inference"] = inference
    return cfg

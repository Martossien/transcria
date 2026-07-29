"""Argv de lancement d'un bot pour une intention — fonctions PURES (testées sans Docker).

Reprend la logique éprouvée de `scripts/bot.sh` : choix d'image par plateforme, variables
d'environnement UNIQUEMENT (jamais un secret en argument, lisible dans `ps`), mode réseau
hôte quand le portail est en loopback, `--shm-size` pour Chromium. Le runner exécute tel
quel — aucune décision au moment du lancement.
"""
from __future__ import annotations

from urllib.parse import urlsplit

DEFAULT_IMAGES = {
    "jitsi": "transcria-bot:latest",
    "zoom-sdk": "transcria-zoom-sdk:latest",
}
_BROWSER_PLATFORMS = frozenset({"jitsi"})


def _portal_is_local(portal_url: str) -> bool:
    host = urlsplit(portal_url).hostname or ""
    return host in ("127.0.0.1", "localhost", "::1")


def docker_argv(intent: dict, *, portal_url: str, token: str,
                images: dict | None = None) -> tuple[list[str], dict[str, str]]:
    """(argv docker run, env du CONTENEUR). L'env est passé par `-e NOM` (valeur héritée du
    process runner, jamais dans argv) — même discipline que bot.sh."""
    provider = str(intent["provider"])
    image = (images or {}).get(provider) or DEFAULT_IMAGES.get(provider)
    if not image:
        raise ValueError(f"aucune image de bot pour la plateforme {provider!r}")
    env = {
        "TRANSCRIA_URL": portal_url,
        "TRANSCRIA_TOKEN": token,
        "TRANSCRIA_JOB_ID": str(intent["job_id"]),
        "BOT_LANGUAGE": str(intent.get("language") or "fr"),
        "BOT_EVENTS": "json",
    }
    argv = ["docker", "run", "--rm"]
    if _portal_is_local(portal_url):
        argv += ["--network", "host"]      # sinon le loopback du conteneur n'est pas l'hôte
    if provider in _BROWSER_PLATFORMS:
        argv += ["--shm-size=1g"]          # Chromium sature les 64 Mo par défaut
    for name in env:
        argv += ["-e", name]               # valeur héritée de l'environnement, pas d'argv
    argv += [image, str(intent["meeting_ref"])]
    return argv, env

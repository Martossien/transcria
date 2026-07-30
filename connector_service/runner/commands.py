"""Argv de lancement d'un bot pour une intention — fonctions PURES (testées sans Docker).

Reprend la logique éprouvée de `scripts/bot.sh` : choix d'image par plateforme, variables
d'environnement UNIQUEMENT (jamais un secret en argument, lisible dans `ps`), mode réseau
hôte quand le portail est en loopback, `--shm-size` pour Chromium. Le runner exécute tel
quel — aucune décision au moment du lancement.
"""
from __future__ import annotations

import os
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
        "TRANSCRIA_PROVIDER": str(intent.get("provider") or "bot"),
        "BOT_LANGUAGE": str(intent.get("language") or "fr"),
        # Politique d'affichage des plateformes : le bot se nomme « fonction — initiateur »
        # (compose_display_name côté bot) — les participants savent QUI l'a envoyé.
        "BOT_INITIATOR": str(intent.get("owner_name") or ""),
        "BOT_EVENTS": "json",
    }
    # Code d'accès d'une salle PROTÉGÉE : par l'ENV uniquement — un secret n'apparaît
    # JAMAIS dans argv (visible de tout `ps` de la machine), même discipline que le jeton.
    passcode = str(intent.get("meeting_passcode") or "")
    if passcode:
        env["BOT_ROOM_PASSCODE"] = passcode
    # Compte d'une instance auto-hébergée exigeant une connexion : propriété de la MACHINE
    # (posé dans l'environnement du runner), jamais une saisie utilisateur — absent partout
    # ailleurs, donc rien à configurer pour meet.jit.si ou une instance ouverte.
    for name in ("JITSI_XMPP_USER", "JITSI_XMPP_PASSWORD"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    argv = ["docker", "run", "--rm"]
    if _portal_is_local(portal_url):
        argv += ["--network", "host"]      # sinon le loopback du conteneur n'est pas l'hôte
    if provider in _BROWSER_PLATFORMS:
        argv += ["--shm-size=1g"]          # Chromium sature les 64 Mo par défaut
    for name in env:
        argv += ["-e", name]               # valeur héritée de l'environnement, pas d'argv
    argv += [image, str(intent["meeting_ref"])]
    return argv, env

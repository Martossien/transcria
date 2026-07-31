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
    "visio": "transcria-visio:latest",
}
_BROWSER_PLATFORMS = frozenset({"jitsi"})
# Identités MACHINE relayées au bot quand elles existent dans l'environnement du runner —
# jamais une saisie utilisateur, jamais dans argv (visible de tout `ps`). Patron établi par
# JITSI_XMPP_* ; LIVEKIT_* = exploitant de l'instance Visio (docs/VISIO_ZOOM_RUNNER.md).
_MACHINE_ENV = (
    "JITSI_XMPP_USER", "JITSI_XMPP_PASSWORD",
    "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET",
    "ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET",       # app Meeting SDK (propriété machine)
    "VISIO_API_BASE", "BOT_HIDDEN",               # visio : API séparée (dev) / bot invisible (opt-in)
    "BOT_IDLE_TIMEOUT_S", "BOT_MAX_DURATION_S",   # réglages génériques des bots (vécu : posés
                                                  # au runner mais jamais relayés au conteneur)
)


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
    for name in _MACHINE_ENV:
        if os.environ.get(name):
            env[name] = os.environ[name]
    # Identités de plateforme remises PAR LE CLAIM (saisies /admin/connecteurs) : elles
    # priment sur l'env machine — c'est l'intention explicite de l'admin. Les VALEURS ne
    # passent que par l'environnement du process (argv ne porte que `-e NOM`).
    for name, value in (intent.get("platform_env") or {}).items():
        if isinstance(name, str) and isinstance(value, str) and value:
            env[name] = value
    argv = ["docker", "run", "--rm"]
    if _portal_is_local(portal_url):
        argv += ["--network", "host"]      # sinon le loopback du conteneur n'est pas l'hôte
    if provider in _BROWSER_PLATFORMS:
        argv += ["--shm-size=1g"]          # Chromium sature les 64 Mo par défaut
    for name in env:
        argv += ["-e", name]               # valeur héritée de l'environnement, pas d'argv
    # Référence de réunion : positionnelle pour les bots qui la prennent ainsi (jitsi,
    # visio) ; par l'ENVIRONNEMENT pour Zoom (son parser n'a pas de positionnel — vécu au
    # premier gate runner : « unrecognized arguments » en boucle) — et un lien Zoom porte
    # un ?pwd= qui n'a rien à faire dans argv (visible de tout `ps`).
    ref_env = {"zoom-sdk": "ZOOM_MEETING"}.get(provider)
    if ref_env:
        env[ref_env] = str(intent["meeting_ref"])
        argv += ["-e", ref_env, image]
        return argv, env
    argv += [image, str(intent["meeting_ref"])]
    return argv, env

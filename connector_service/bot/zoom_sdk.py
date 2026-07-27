"""Bot ZOOM par le Meeting SDK natif — `python -m connector_service.bot.zoom_sdk`.

ENTRYPOINT de l'image `Dockerfile.zoom-sdk` : un conteneur, une réunion. Le SDK n'admet qu'une
instance par processus (`SDKERR_OTHER_SDK_INSTANCE_RUNNING`), donc ce découpage n'est pas un
choix d'exploitation mais une contrainte de la bibliothèque.

Mêmes conventions que le bot navigateur (`connector_service.bot.cli`) — variables
d'environnement d'abord, options équivalentes pour la main, et MÊMES codes de retour, pour que
l'orchestration n'ait pas à distinguer les plateformes :

    0  la réunion s'est déroulée
    1  la réunion n'a pas pu être rejointe (identifiants, code, salle d'attente sans réponse)
    2  ANOMALIE technique → rejouable
    3  erreur de configuration — inutile de rejouer tel quel

Le Client Secret n'est lu QUE depuis l'environnement : le passer en option le rendrait visible
dans la liste des processus de la machine.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
from urllib.parse import parse_qs, urlsplit

from connector_service.bot.cli import (
    EXIT_CONFIG,
    EXIT_NOT_ADMITTED,
    EXIT_OK,
    EXIT_TECHNICAL,
    build_transcriber,
)
from connector_service.contract import ExternalMeetingOccurrence
from connector_service.live._demux import DemuxFrameSource
from connector_service.live.media import LiveAudioProvider
from connector_service.live.session import LiveSession
from connector_service.live.zoom_sdk_state import ZoomSdkPhase
from connector_service.live.zoom_sdk_transport import (
    SAMPLING_RATE_32K,
    ZoomSdkError,
    zoom_sdk_demux_source,
)
from connector_service.signatures import normalize_meeting_number

logger = logging.getLogger("connector_service.bot.zoom_sdk")


def parse_zoom_invite(value: str) -> tuple[str, str]:
    """Lien d'invitation OU numéro brut → (numéro de réunion, code secret).

    Fonction PURE, donc testée. Elle existe parce qu'un utilisateur transmet ce qu'il a sous
    la main : « 578 629 7113 », ou le lien complet, dont le code est dans `?pwd=`. Exiger une
    forme précise ferait échouer l'entrée pour une raison sans rapport avec la réunion.

    Le code porté par un lien est un code CHIFFRÉ propre à Zoom : il est transmis tel quel,
    c'est ce que le SDK attend.
    """
    text = (value or "").strip()
    if not text:
        raise ValueError("numéro de réunion ou lien d'invitation requis")
    if "://" not in text:
        return normalize_meeting_number(text), ""

    parts = urlsplit(text)
    segments = [segment for segment in parts.path.split("/") if segment]
    digits = next((segment for segment in reversed(segments)
                   if segment.isdigit()), "")
    if not digits:
        raise ValueError(f"aucun numéro de réunion lisible dans : {text}")
    passcode = (parse_qs(parts.query).get("pwd") or [""])[0]
    return normalize_meeting_number(digits), passcode


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        logger.warning("%s ignoré (nombre attendu) : %r", name, raw)
        return default


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="connector_service.bot.zoom_sdk",
        description="Rejoint une réunion Zoom via le Meeting SDK natif et transcrit l'audio "
                    "par participant.")
    parser.add_argument("--meeting", default=os.environ.get("ZOOM_MEETING"),
                        help="numéro de réunion ou lien d'invitation (ou ZOOM_MEETING)")
    parser.add_argument("--passcode", default=os.environ.get("ZOOM_PASSCODE", ""),
                        help="code secret, s'il n'est pas déjà dans le lien")
    parser.add_argument("--client-id", default=os.environ.get("ZOOM_CLIENT_ID"),
                        help="Client ID de l'app Meeting SDK (ou ZOOM_CLIENT_ID)")
    parser.add_argument("--transcria-url", default=os.environ.get("TRANSCRIA_URL"),
                        help="URL de TranscrIA pour la transcription (ou TRANSCRIA_URL)")
    parser.add_argument("--token", default=os.environ.get("TRANSCRIA_TOKEN"),
                        help="jeton d'API TranscrIA (ou TRANSCRIA_TOKEN)")
    parser.add_argument("--name", default=os.environ.get("BOT_DISPLAY_NAME", "TranscrIA"),
                        help="nom affiché du bot dans la réunion")
    parser.add_argument("--language", default=os.environ.get("BOT_LANGUAGE") or None,
                        help="langue de transcription (ex. fr)")
    parser.add_argument("--sampling-rate-hz", type=int,
                        default=int(_env_float("ZOOM_SAMPLING_RATE_HZ", SAMPLING_RATE_32K)),
                        help="débit de l'audio brut : 32000 ou 48000 (le SDK n'offre pas 16 kHz)")
    parser.add_argument("--admission-timeout-s", type=float,
                        default=_env_float("BOT_ADMISSION_TIMEOUT_S", 300.0),
                        help="attente en salle d'attente avant d'abandonner")
    parser.add_argument("--max-duration-s", type=float,
                        default=_env_float("BOT_MAX_DURATION_S", 4 * 3600),
                        help="durée maximale de présence en réunion")
    # Réunions EXTERNES au compte propriétaire de l'app : exigent en plus une revue de l'app
    # par Zoom. Les emplacements existent, mais aucun code ne dispense de cette revue.
    parser.add_argument("--zak", default=os.environ.get("ZOOM_ZAK", ""),
                        help="jeton ZAK (réunion hors du compte de l'app)")
    parser.add_argument("--on-behalf-token", default=os.environ.get("ZOOM_OBF_TOKEN", ""),
                        help="jeton OBF (réunion hors du compte de l'app)")
    return parser


async def run(args: argparse.Namespace, client_secret: str) -> int:
    meeting_number, invite_passcode = parse_zoom_invite(args.meeting)
    passcode = args.passcode or invite_passcode

    occurrence = ExternalMeetingOccurrence(
        provider="zoom", provider_account_id=os.environ.get("BOT_ACCOUNT", "zoom-sdk"),
        external_occurrence_id=meeting_number)

    # La phase la PLUS AVANCÉE atteinte détermine le code de retour : « jamais entré » et
    # « entré puis sorti » ne se rejouent pas de la même façon.
    reached: dict[str, ZoomSdkPhase] = {"phase": ZoomSdkPhase.CONNECTING}

    def _on_phase(phase: ZoomSdkPhase) -> None:
        if phase is ZoomSdkPhase.ACTIVE:
            reached["phase"] = phase

    source = DemuxFrameSource(zoom_sdk_demux_source(
        args.client_id, client_secret, meeting_number,
        display_name=args.name, passcode=passcode,
        sampling_rate_hz=args.sampling_rate_hz,
        zak=args.zak, on_behalf_token=args.on_behalf_token,
        admission_timeout_s=args.admission_timeout_s,
        on_phase=_on_phase))
    provider = LiveAudioProvider("zoom", source)
    transcriber = build_transcriber(args.transcria_url, args.token, args.language)

    segments: list = []
    session = LiveSession(transcriber, on_final=segments.append)

    logger.info("Bot Zoom en route | réunion=%s durée_max=%.0fs",
                meeting_number, args.max_duration_s)
    try:
        await asyncio.wait_for(session.run(provider, occurrence),
                               timeout=args.max_duration_s)
    except asyncio.TimeoutError:
        logger.info("Durée maximale atteinte — sortie de réunion")
    except ZoomSdkError as exc:
        # Échec d'entrée : le message porte déjà le diagnostic Zoom traduit.
        logger.error("Zoom : %s", exc)
        return EXIT_NOT_ADMITTED

    logger.info("Réunion terminée | segments=%d", len(segments))
    for segment in segments:
        print(f"[{segment.speaker or '?'}] {segment.text}", flush=True)

    if reached["phase"] is not ZoomSdkPhase.ACTIVE:
        return EXIT_NOT_ADMITTED
    return EXIT_OK


def exit_immediately(code: int) -> None:
    """Termine le processus SANS finalisation de l'interpréteur.

    POURQUOI CE DÉTOUR : le SDK Zoom plante par segfault dans ses destructeurs statiques, à la
    sortie de l'interpréteur — APRÈS que tout le travail utile est fait, session fermée et
    `CleanUPSDK()` appelé (observé en réunion réelle : capture complète et réussie, puis code
    de sortie 139). Rien côté Python ne peut l'empêcher, la faute étant dans du code natif que
    nous ne contrôlons pas.

    Le laisser se produire coûterait cher : les codes de retour sont le CONTRAT avec
    l'orchestration (0 = la réunion a eu lieu, 2 = anomalie rejouable). Une réunion
    parfaitement transcrite ressemblerait à une panne, et serait rejouée pour rien.

    `os._exit` est acceptable ICI et seulement ici : le processus est un conteneur jetable
    dédié à UNE réunion, il n'a rien à finaliser. Les tampons sont vidés explicitement, car
    `os._exit` ne le fait pas.
    """
    import os

    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.flush()
    os._exit(code)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get("BOT_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)

    # Le secret n'est JAMAIS une option de ligne de commande : il apparaîtrait dans la liste
    # des processus, lisible par tout utilisateur de la machine.
    client_secret = os.environ.get("ZOOM_CLIENT_SECRET", "")

    missing = [name for name, value in (
        ("--meeting / ZOOM_MEETING", args.meeting),
        ("--client-id / ZOOM_CLIENT_ID", args.client_id),
        ("ZOOM_CLIENT_SECRET", client_secret),
    ) if not value]
    if missing:
        logger.error("configuration incomplète : %s", ", ".join(missing))
        return EXIT_CONFIG

    try:
        return asyncio.run(run(args, client_secret))
    except ValueError as exc:                    # lien/numéro illisible
        logger.error("%s", exc)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        logger.info("Interruption demandée")
        return EXIT_OK
    except ZoomSdkError as exc:
        logger.error("Zoom : %s", exc)
        return EXIT_TECHNICAL


if __name__ == "__main__":
    # `exit_immediately` plutôt que `sys.exit` : cf. sa docstring — le SDK plante à la
    # finalisation de l'interpréteur, ce qui masquerait un succès derrière un code 139.
    exit_immediately(main())

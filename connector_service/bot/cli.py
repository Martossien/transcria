"""Ligne de commande du bot de réunion — un conteneur, une réunion.

Conçue pour l'exploitation, pas pour la mise au point : tout se règle par VARIABLES
D'ENVIRONNEMENT (ce que fournissent Docker, Kubernetes et systemd), avec des options
équivalentes pour un lancement à la main. Le code de retour distingue les issues, ce qui
permet à l'orchestrateur de décider s'il faut rejouer la réunion :

    0  la réunion s'est déroulée (fin normale, bot parti seul, réunion close, expulsé)
    1  la réunion n'a pas pu être rejointe (refus, mot de passe, authentification)
    2  ANOMALIE technique (plus de média, transport coupé, navigateur perdu) → rejouable
    3  erreur de configuration (paramètre manquant) — inutile de rejouer tel quel
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

from connector_service.bot.platforms.jitsi import JitsiDriver
from connector_service.bot.runner import run_bot_session
from connector_service.contract import ExternalMeetingOccurrence
from connector_service.live.facade_client import facade_transcriber
from connector_service.live.facade_stt import FacadeTranscriber

logger = logging.getLogger("connector_service.bot")

EXIT_OK = 0
EXIT_NOT_ADMITTED = 1
EXIT_TECHNICAL = 2
EXIT_CONFIG = 3

# Issues qui signifient « la réunion a eu lieu » : rejouer n'apporterait rien.
_COMPLETED_REASONS = frozenset({"left_alone", "removed", "conference_ended",
                                "max_duration", "stopped"})


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        logger.warning("%s ignoré (nombre attendu) : %r", name, raw)
        return default


# --------------------------------------------------------------------------- #
#  Nom affiché du bot — obligation de la politique d'usage des bots de Zoom
# --------------------------------------------------------------------------- #
DEFAULT_FUNCTION = "Transcription"
DEFAULT_PRODUCT = "TranscrIA"


def compose_display_name(*, explicit: str = "", initiator: str = "",
                         function: str = DEFAULT_FUNCTION,
                         product: str = DEFAULT_PRODUCT) -> str:
    """Nom sous lequel le bot se présente aux participants — fonction PURE, donc testée.

    Zoom l'EXIGE : un outil automatisé doit s'afficher « labeled with the name of the user
    who initiated it and its function » (exemple donné par Zoom : « Steve Miller's notetaking
    app »). Un nom de produit seul — « TranscrIA » — ne dit ni qui l'a mis là, ni ce qu'il
    fait, et laisse les participants face à un inconnu qui enregistre.

    Ce n'est pas de la cosmétique : la politique d'usage des bots porte sur l'INFORMATION des
    participants, et c'est le seul de ses points que nous ne satisfaisions pas.

    `explicit` l'emporte toujours : une organisation peut avoir ses propres règles de
    nommage, et nous n'avons pas à les contredire.
    """
    if explicit:
        return explicit
    if initiator:
        return f"{function} — {initiator}"
    # Sans initiateur connu, on nomme au moins la FONCTION : « TranscrIA » seul
    # n'apprend rien à qui découvre ce participant dans la liste.
    return f"{product} — {function.lower()}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="connector_service.bot",
        description="Rejoint une réunion, capte l'audio par participant et le transcrit.")
    parser.add_argument("meeting_url", nargs="?", default=os.environ.get("MEETING_URL"),
                        help="URL de la réunion (ou variable MEETING_URL)")
    parser.add_argument("--transcria-url", default=os.environ.get("TRANSCRIA_URL"),
                        help="URL de TranscrIA pour la transcription (ou TRANSCRIA_URL)")
    parser.add_argument("--token", default=os.environ.get("TRANSCRIA_TOKEN"),
                        help="jeton d'API TranscrIA (ou TRANSCRIA_TOKEN)")
    parser.add_argument("--name", default=os.environ.get("BOT_DISPLAY_NAME", ""),
                        help="nom affiché — à défaut, composé depuis --initiator")
    parser.add_argument("--initiator", default=os.environ.get("BOT_INITIATOR", ""),
                        help="personne à l'origine de la demande (Zoom exige que le nom "
                             "affiché désigne l'initiateur et la fonction)")
    parser.add_argument("--language", default=os.environ.get("BOT_LANGUAGE") or None,
                        help="langue de transcription (ex. fr)")
    parser.add_argument("--max-duration-s", type=float,
                        default=_env_float("BOT_MAX_DURATION_S", 4 * 3600),
                        help="durée maximale de présence en réunion")
    parser.add_argument("--alone-timeout-s", type=float,
                        default=_env_float("BOT_ALONE_TIMEOUT_S", 30.0),
                        help="durée SEUL en réunion avant de repartir")
    parser.add_argument("--admission-timeout-s", type=float,
                        default=_env_float("BOT_ADMISSION_TIMEOUT_S", 120.0),
                        help="attente en salle d'attente avant d'abandonner")
    parser.add_argument("--insecure", action="store_true",
                        default=bool(os.environ.get("BOT_INSECURE")),
                        help="accepte un certificat auto-signé (instance interne)")
    return parser


class _NullTranscriber:
    """Transcripteur inerte : le bot capte sans transcrire (diagnostic, ou transcription
    confiée au traitement post-réunion)."""

    uses_local_agreement = False

    async def stream(self, frames):
        async for _ in frames:
            pass
        return
        yield  # pragma: no cover — générateur sans émission


def _json_event_emitter():
    """BOT_EVENTS=json (vague 4) : une ligne JSON par transition d'état sur stdout — le
    meeting-runner les relaie au portail (états « salle d'attente », « en réunion » sur la
    carte du job) sans parser les logs. Mapping vers les événements de /v1/meetings/events."""
    mapping = {"joining": "joining", "waiting_admission": "waiting_admission",
               "active": "in_meeting"}

    def emit(state) -> None:
        event = mapping.get(getattr(state, "value", str(state)))
        if event:
            print(json.dumps({"bot_event": event}), flush=True)
    return emit


def build_transcriber(transcria_url: str | None, token: str | None, language: str | None):
    """Transcripteur réel si TranscrIA est joignable, sinon capture seule (jamais d'échec
    au lancement pour autant : un bot qui capte sans transcrire reste utile)."""
    if not transcria_url:
        logger.warning("TRANSCRIA_URL absent — capture SANS transcription")
        return _NullTranscriber()
    return FacadeTranscriber(facade_transcriber(transcria_url, token or "", language=language))


def exit_code_for(admitted: bool, reason: str) -> int:
    """Traduit l'issue de la réunion en code de retour exploitable par l'orchestrateur."""
    if not admitted:
        return EXIT_NOT_ADMITTED
    return EXIT_OK if reason in _COMPLETED_REASONS else EXIT_TECHNICAL


async def run(args: argparse.Namespace) -> int:
    occurrence = ExternalMeetingOccurrence(
        provider="bot", provider_account_id=os.environ.get("BOT_ACCOUNT", "bot"),
        external_occurrence_id=args.meeting_url.rstrip("/").rsplit("/", 1)[-1])
    driver = JitsiDriver("", headless=True, ignore_https_errors=args.insecure,
                         alone_timeout_s=args.alone_timeout_s,
                         max_duration_s=args.max_duration_s)
    transcriber = build_transcriber(args.transcria_url, args.token, args.language)

    logger.info("Bot en route | réunion=%s durée_max=%.0fs", args.meeting_url,
                args.max_duration_s)
    on_state = _json_event_emitter() if os.environ.get("BOT_EVENTS") == "json" else None
    outcome, segments = await run_bot_session(
        args.meeting_url, occurrence, driver, transcriber,
        display_name=compose_display_name(explicit=args.name, initiator=args.initiator),
        on_state=on_state)

    logger.info("Réunion terminée | admis=%s motif=%s%s segments=%d",
                outcome.admitted, outcome.reason,
                f" ({outcome.detail})" if outcome.detail else "", len(segments))
    for segment in segments:
        print(f"[{segment.speaker or '?'}] {segment.text}", flush=True)
    return exit_code_for(outcome.admitted, outcome.reason)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get("BOT_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    if not args.meeting_url:
        logger.error("URL de réunion manquante (argument, ou variable MEETING_URL)")
        return EXIT_CONFIG
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        logger.info("Interruption demandée")
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

"""Bot VISIO (La Suite) par client LiveKit — `python -m connector_service.bot.visio`.

ENTRYPOINT de l'image `Dockerfile.visio` : un conteneur, une réunion — PAS de navigateur.
Le bot rejoint la room LiveKit comme participant CACHÉ auditeur (`livekit_access_token` :
`can_subscribe` seul, `can_publish=False` refusé au niveau du serveur) et reçoit l'audio
démultiplexé PAR PARTICIPANT — les pistes séparées v2 y sont natives (aucun pont JS).

Mêmes conventions que les bots navigateur (`bot/cli.py`) et Zoom SDK : variables
d'environnement d'abord, MÊMES codes de retour (0/1/2/3), `BOT_EVENTS=json`, captions du
suivi en direct, rattachement au job planifié (`TRANSCRIA_JOB_ID`) — toute la plomberie
vient du socle commun `bot/_workflow` (lot V0).

Identités machine (patron `JITSI_XMPP_*`, relayées par le meeting-runner) :
`LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` — la voie VALIDÉE du catalogue est
celle de l'EXPLOITANT de l'instance Visio, qui forge lui-même le jeton du bot.

Nom de room : VÉRIFIÉ contre la source officielle (`~/reference/meet`,
`core/api/viewsets.py:271`) — pour une salle publique, la room LiveKit est le SLUG de
l'URL (`slugify(pk)`) ; `parse_visio_room` applique la même normalisation.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
import unicodedata
from urllib.parse import urlsplit

from connector_service.bot._workflow import (
    ingest_recording,
    json_caption_emitter,
    json_event_emitter,
)
from connector_service.bot.cli import (
    EXIT_CONFIG,
    EXIT_NOT_ADMITTED,
    EXIT_OK,
    EXIT_TECHNICAL,
    build_transcriber,
    compose_display_name,
)
from connector_service.contract import ExternalMeetingOccurrence
from connector_service.live._demux import DemuxFrameSource
from connector_service.live.livekit_source import livekit_access_token
from connector_service.live.livekit_transport import livekit_demux_source
from connector_service.live.media import LiveAudioProvider
from connector_service.live.recorder import RecordingTee
from connector_service.live.session import LiveSession
from connector_service.outbound_guard import (
    HoteRefuse,
    ouvreur_sans_redirection,
    verifier_hote_sortant,
)

logger = logging.getLogger("connector_service.bot.visio")

_SAMPLE_RATE_HZ = 16000        # livekit_demux_source force 16 kHz/mono à la création du stream


def parse_visio_room(value: str) -> str:
    """Lien de salle OU nom brut → nom de room LiveKit. Fonction PURE, testée.

    Même normalisation que le backend Visio (`slugify` Django sur le dernier segment du
    chemin — référence `~/reference/meet` `core/api/viewsets.py:271`) : minuscules, accents
    retirés, tout ce qui n'est pas alphanumérique devient un tiret unique."""
    text = (value or "").strip()
    if not text:
        raise ValueError("lien ou nom de salle Visio requis")
    if "://" in text:
        segments = [s for s in urlsplit(text).path.split("/") if s]
        if not segments:
            raise ValueError(f"aucun nom de salle lisible dans : {text}")
        text = segments[-1]
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    if not slug:
        raise ValueError(f"nom de salle vide après normalisation : {value!r}")
    return slug


def resolve_livekit_room(meeting_ref: str, opener=None) -> str:
    """Nom de room LiveKit RÉEL d'un lien de salle. Vérifié dans la source officielle
    (~/reference/meet) : une salle ENREGISTRÉE expose sa room comme l'UUID de la salle
    (`serializers.py:179`) — le slug d'URL ne vaut que pour les salles éphémères
    (`viewsets.py:271`). On interroge donc l'API du MÊME hôte (`/api/v1.0/rooms/<slug>/`,
    anonyme) ; repli honnête sur le slug (salle éphémère, API indisponible, nom brut)."""
    import json as _json

    slug = parse_visio_room(meeting_ref)
    if "://" not in (meeting_ref or ""):
        return slug
    parts = urlsplit(meeting_ref)
    # VISIO_API_BASE : stack de DEV officielle = front (3000) et API Django (8071) sur des
    # ports distincts — les vraies instances servent tout sur le même hôte (défaut).
    base_exploitant = os.environ.get("VISIO_API_BASE", "").rstrip("/")
    base = base_exploitant or f"{parts.scheme}://{parts.netloc}"
    api = f"{base}/api/v1.0/rooms/{slug}/"

    # S2.2 — la garde ne s'applique QUE si l'hôte vient du lien de l'utilisateur.
    # `VISIO_API_BASE` est une valeur d'EXPLOITANT : elle vise légitimement la machine
    # locale (c'est la stack de développement officielle, front 3000 / API 8071), et la
    # contrôler reviendrait à se protéger de soi-même. La distinction est tout l'objet de
    # ce correctif : on borne ce que l'utilisateur choisit, pas ce que l'exploitant règle.
    if not base_exploitant:
        try:
            verifier_hote_sortant(api)
        except HoteRefuse as exc:
            logger.warning("Résolution de salle refusée (%s) — room = slug « %s »", exc, slug)
            return slug

    def _default(url):
        # Ouvreur qui NE SUIT PAS les redirections : `urlopen` les suit par défaut, donc un
        # hôte légitime répondant `302 Location: http://127.0.0.1/` contournait la
        # vérification faite juste au-dessus. Vérifier puis laisser la bibliothèque aller
        # ailleurs, c'est ne pas vérifier.
        with ouvreur_sans_redirection().open(url, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    try:
        status, body = (opener or _default)(api)
        room = str((_json.loads(body).get("livekit") or {}).get("room") or "")
        if status == 200 and room:
            logger.info("Salle « %s » résolue en room LiveKit %s (salle enregistrée)",
                        slug, room)
            return room
    except Exception as exc:  # noqa: BLE001 — repli slug, jamais un échec ici
        logger.info("API rooms injoignable (%s) — room = slug « %s »", exc, slug)
    return slug


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="connector_service.bot.visio",
        description="Rejoint une salle Visio (LiveKit) en auditeur caché et transcrit "
                    "l'audio par participant.")
    parser.add_argument("meeting_ref", nargs="?", default=os.environ.get("MEETING_URL"),
                        help="lien de la salle Visio ou nom de room (ou MEETING_URL)")
    parser.add_argument("--livekit-url", default=os.environ.get("LIVEKIT_URL"),
                        help="URL du serveur LiveKit (ou LIVEKIT_URL)")
    parser.add_argument("--transcria-url", default=os.environ.get("TRANSCRIA_URL"),
                        help="URL de TranscrIA pour la transcription (ou TRANSCRIA_URL)")
    parser.add_argument("--token", default=os.environ.get("TRANSCRIA_TOKEN"),
                        help="jeton d'API TranscrIA (ou TRANSCRIA_TOKEN)")
    parser.add_argument("--name", default=os.environ.get("BOT_DISPLAY_NAME", ""),
                        help="nom affiché — à défaut, composé depuis --initiator")
    parser.add_argument("--initiator", default=os.environ.get("BOT_INITIATOR", ""),
                        help="personne à l'origine de la demande")
    parser.add_argument("--language", default=os.environ.get("BOT_LANGUAGE") or None,
                        help="langue de transcription (ex. fr)")
    parser.add_argument("--max-duration-s", type=float,
                        default=float(os.environ.get("BOT_MAX_DURATION_S") or 4 * 3600),
                        help="durée maximale de présence en réunion")
    parser.add_argument("--idle-timeout-s", type=float,
                        default=float(os.environ.get("BOT_IDLE_TIMEOUT_S") or 900.0),
                        help="silence audio total avant de quitter (un bot CACHÉ maintient "
                             "la room ouverte : sans cette garde, une salle désertée ne se "
                             "fermerait jamais)")
    return parser


def monitored_frames(factory, *, on_first=None, idle_timeout_s: float = 900.0,
                     clock=None):
    """Enveloppe une fabrique de frames : signale la PREMIÈRE frame (→ état `in_meeting`,
    le seul signal fiable « on est vraiment dedans ») et clôt le flux après
    `idle_timeout_s` sans frame — compromis assumé et journalisé : quand tout le monde est
    parti, plus aucune piste n'émet ; une réunion entière muette aussi longtemps est
    irréaliste. Testée avec une fabrique factice."""
    def wrap(occurrence):
        async def _gen():
            iterator = factory(occurrence).__aiter__()
            first = True
            while True:
                try:
                    frame = await asyncio.wait_for(iterator.__anext__(),
                                                   timeout=idle_timeout_s)
                except StopAsyncIteration:
                    return
                except asyncio.TimeoutError:
                    logger.info("aucune frame audio depuis %.0f s — salle vraisemblablement "
                                "désertée, sortie", idle_timeout_s)
                    return
                if first and on_first is not None:
                    on_first()
                    first = False
                yield frame
        return _gen()
    return wrap


async def run(args: argparse.Namespace, api_key: str, api_secret: str) -> int:
    room = resolve_livekit_room(args.meeting_ref)
    occurrence = ExternalMeetingOccurrence(
        provider="visio", provider_account_id=os.environ.get("BOT_ACCOUNT", "visio"),
        external_occurrence_id=room)
    events_json = os.environ.get("BOT_EVENTS") == "json"
    emit_state = json_event_emitter() if events_json else (lambda _s: None)
    emit_caption = json_caption_emitter() if events_json else None

    # TRANSPARENCE (demande utilisateur, gate 2026-07-31) : le bot est VISIBLE par
    # défaut — les participants DOIVENT savoir qu'une captation a lieu (le nom dit qui
    # l'a demandée). BOT_HIDDEN=1 = opt-in explicite de l'exploitant, assumé par lui.
    access = livekit_access_token(
        api_key, api_secret, room,
        name=compose_display_name(explicit=args.name, initiator=args.initiator),
        hidden=os.environ.get("BOT_HIDDEN") == "1")
    reached = {"in_meeting": False}

    def _on_first_frame() -> None:
        reached["in_meeting"] = True
        emit_state("active")

    # DemuxFrameSource synthétise séquence + horloge média PAR participant (les frames
    # LiveKit n'en portent pas) et rend les RawFrame du contrat — même montage que Zoom.
    source = DemuxFrameSource(monitored_frames(
        livekit_demux_source(args.livekit_url, access),
        on_first=_on_first_frame, idle_timeout_s=args.idle_timeout_s))
    provider = LiveAudioProvider("visio", source)
    transcriber = build_transcriber(args.transcria_url, args.token, args.language)

    target_job_id = (os.environ.get("TRANSCRIA_JOB_ID") or "").strip() or None
    recording = None
    if target_job_id and args.transcria_url:
        recording = RecordingTee(transcriber, sample_rate_hz=_SAMPLE_RATE_HZ)
        transcriber = recording

    segments: list = []

    def _on_final(seg) -> None:
        segments.append(seg)
        if emit_caption is not None:
            emit_caption(seg)

    session = LiveSession(transcriber, on_final=_on_final)

    logger.info("Bot Visio en route | room=%s durée_max=%.0fs%s", room,
                args.max_duration_s, f" job={target_job_id}" if target_job_id else "")
    emit_state("joining")
    try:
        await asyncio.wait_for(session.run(provider, occurrence),
                               timeout=args.max_duration_s)
    except asyncio.TimeoutError:
        logger.info("Durée maximale atteinte — sortie de la salle")
    except Exception as exc:  # noqa: BLE001 — connexion LiveKit : le code de retour décide du rejeu
        logger.error("Visio/LiveKit : %r", exc)
        return EXIT_TECHNICAL if reached["in_meeting"] else EXIT_NOT_ADMITTED

    logger.info("Réunion terminée | segments=%d", len(segments))
    for segment in segments:
        print(f"[{segment.speaker or '?'}] {segment.text}", flush=True)

    if not reached["in_meeting"]:
        # Connexion réussie mais AUCUNE frame : salle jamais occupée pendant la fenêtre —
        # « non admis » au sens du contrat (rejouer à l'identique ne donnerait rien).
        return EXIT_NOT_ADMITTED
    if recording is not None and target_job_id and recording.mixer.duration_s > 0:
        await ingest_recording(args.transcria_url, args.token or "", occurrence,
                               recording, target_job_id)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get("BOT_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    api_key = os.environ.get("LIVEKIT_API_KEY", "")
    api_secret = os.environ.get("LIVEKIT_API_SECRET", "")
    if not args.meeting_ref:
        logger.error("salle Visio manquante (argument, ou variable MEETING_URL)")
        return EXIT_CONFIG
    if not (args.livekit_url and api_key and api_secret):
        logger.error("identités LiveKit manquantes — poser LIVEKIT_URL, LIVEKIT_API_KEY et "
                     "LIVEKIT_API_SECRET dans l'environnement du meeting-runner (propriété "
                     "de la machine, cf. /admin/connecteurs)")
        return EXIT_CONFIG
    try:
        return asyncio.run(run(args, api_key, api_secret))
    except KeyboardInterrupt:
        logger.info("Interruption demandée")
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

"""Racine de composition du bot (gate manuel) — câble navigateur → pont → session live.

Fait tourner de concert : (1) le serveur du pont qui reçoit la connexion du payload de capture,
(2) l'orchestrateur qui pilote le navigateur (join/admission/leave), (3) la `LiveSession` qui
consomme le PCM du pont → segments à provenance. Le pipeline aval (façade/ingest) est le même
que pour les transports officiels. NON testable en CI (navigateur réel) — documenté et confirmé
au gate sur Jitsi.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from connector_service.bot.bridge_server import connection_messages, serve_bot_bridge
from connector_service.bot.orchestrator import BotOutcome, BotSession, BrowserDriver
from connector_service.contract import ExternalMeetingOccurrence
from connector_service.live.bridge_source import MediaBridgeFrameSource
from connector_service.live.media import LiveAudioProvider
from connector_service.live.session import LiveSession

_log = logging.getLogger(__name__)


async def run_bot_session(meeting_url: str, occurrence: ExternalMeetingOccurrence,
                          driver: BrowserDriver, transcriber: Any, *,
                          provider_name: str = "bot", bridge_host: str = "127.0.0.1",
                          bridge_port: int = 8791) -> tuple[BotOutcome, list]:
    """Déroule une réunion via bot : le payload JS pousse le PCM sur le pont, une `LiveSession`
    le transcrit pendant que l'orchestrateur pilote le navigateur. Retourne (issue, segments)."""
    connected: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    session_done = asyncio.Event()
    segments: list = []

    async def _on_connection(conn: Any) -> None:
        # websockets FERME la connexion dès que le handler retourne : on la garde vivante
        # pendant toute la session (sinon aucun audio n'arrive jamais). Connexion en trop = fermée.
        if not connected.done():
            connected.set_result(conn)
            await session_done.wait()

    server = await serve_bot_bridge(bridge_host, bridge_port, _on_connection)

    async def _transcribe() -> None:
        conn = await connected                       # attend que le navigateur se connecte
        source = MediaBridgeFrameSource(connection_messages(conn))
        provider = LiveAudioProvider(provider_name, source)
        session = LiveSession(transcriber, on_final=segments.append)
        await session.run(provider, occurrence)

    transcribe_task = asyncio.ensure_future(_transcribe())
    try:
        outcome = await BotSession(driver).run(meeting_url)   # pilote le navigateur
        if not connected.done():
            connected.cancel()                       # jamais admis → débloque le transcripteur
        results = await asyncio.gather(transcribe_task, return_exceptions=True)
        for res in results:                          # ne pas avaler une vraie erreur en silence
            if isinstance(res, BaseException) and not isinstance(res, asyncio.CancelledError):
                _log.warning("transcription du bot interrompue: %r", res)
        return outcome, segments
    finally:
        transcribe_task.cancel()                     # erreur du driver → pas de task fantôme
        with contextlib.suppress(asyncio.CancelledError):
            await transcribe_task
        session_done.set()                           # libère le handler du pont
        server.close()
        await server.wait_closed()

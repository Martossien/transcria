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
from collections.abc import AsyncIterator
from typing import Any

from connector_service.bot.bridge_server import decode_bridge_message, serve_bot_bridge
from connector_service.bot.orchestrator import BotOutcome, BotSession, BrowserDriver
from connector_service.contract import ExternalMeetingOccurrence
from connector_service.live.bridge_source import MediaBridgeFrameSource
from connector_service.live.media import LiveAudioProvider
from connector_service.live.session import LiveSession

_log = logging.getLogger(__name__)


async def run_bot_session(meeting_url: str, occurrence: ExternalMeetingOccurrence,
                          driver: BrowserDriver, transcriber: Any, *,
                          provider_name: str = "bot", display_name: str = "TranscrIA",
                          bridge_host: str = "127.0.0.1",
                          bridge_port: int = 8791) -> tuple[BotOutcome, list]:
    """Déroule une réunion via bot : le payload JS pousse le PCM sur le pont, une `LiveSession`
    le transcrit pendant que l'orchestrateur pilote le navigateur. Retourne (issue, segments)."""
    # Le payload de capture se (re)connecte PLUSIEURS fois au cours d'une réunion : la page
    # navigue (accueil → conférence), il y a des iframes, et il se reconnecte s'il est coupé.
    # On MULTIPLEXE donc toutes les connexions dans une file unique — n'en garder qu'une
    # (« la première gagne ») ferait rejeter la vraie page de conférence. Chaque handler reste
    # vivant tant que sa connexion vit (websockets ferme dès que le handler retourne).
    inbox: asyncio.Queue = asyncio.Queue()
    segments: list = []
    stats = {"connections": 0, "messages": 0}
    end_of_stream = object()

    async def _on_connection(conn: Any) -> None:
        stats["connections"] += 1
        try:
            async for raw in conn:
                stats["messages"] += 1
                await inbox.put(raw)
        except Exception as exc:  # noqa: BLE001 — une connexion morte n'arrête pas la session
            _log.debug("connexion du pont terminée: %r", exc)

    server = await serve_bot_bridge(bridge_host, bridge_port, _on_connection)

    async def _messages(_occurrence: ExternalMeetingOccurrence) -> AsyncIterator[dict]:
        while True:
            item = await inbox.get()
            if item is end_of_stream:
                return
            for msg in decode_bridge_message(item):
                yield msg

    async def _transcribe() -> None:
        source = MediaBridgeFrameSource(_messages)
        provider = LiveAudioProvider(provider_name, source)
        session = LiveSession(transcriber, on_final=segments.append)
        await session.run(provider, occurrence)

    transcribe_task = asyncio.ensure_future(_transcribe())
    try:
        outcome = await BotSession(driver, display_name=display_name).run(meeting_url)
        await inbox.put(end_of_stream)               # fin de réunion → clôt la transcription
        results = await asyncio.gather(transcribe_task, return_exceptions=True)
        for res in results:                          # ne pas avaler une vraie erreur en silence
            if isinstance(res, BaseException) and not isinstance(res, asyncio.CancelledError):
                _log.warning("transcription du bot interrompue: %r", res)
        _log.info("pont : %d connexion(s), %d message(s)",
                  stats["connections"], stats["messages"])
        return outcome, segments
    finally:
        transcribe_task.cancel()                     # erreur du driver → pas de task fantôme
        with contextlib.suppress(asyncio.CancelledError):
            await transcribe_task
        server.close()
        await server.wait_closed()

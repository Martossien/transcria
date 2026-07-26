"""Serveur du pont côté bot — reçoit le PCM poussé par le payload JS du navigateur.

Le payload de capture in-page (WebRTC → WebCodecs) ouvre une WebSocket vers CE serveur local
et pousse des messages JSON au format du pont (`connector_service.live.bridge_source`). Une
connexion = une réunion = une occurrence. Le décodage est testable en CI ; l'écoute WebSocket
réelle (`websockets.serve`) est thin, confirmée au gate.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from connector_service.contract import ExternalMeetingOccurrence


def decode_bridge_message(raw: Any) -> list[dict]:
    """Un message WS brut (str/bytes JSON) → [dict] ou [] si illisible (frame corrompue,
    message de contrôle…). Sous forme de liste pour composer sans exception."""
    try:
        msg = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return [msg] if isinstance(msg, dict) else []


async def decode_bridge_frames(raw_stream: AsyncIterator[Any]) -> AsyncIterator[dict]:
    """Messages WS bruts (str/bytes JSON) → dicts. Ignore le JSON illisible (frame corrompue)
    sans casser le flux."""
    async for raw in raw_stream:
        for msg in decode_bridge_message(raw):
            yield msg


def connection_messages(raw_stream: AsyncIterator[Any]):
    """Adapte le flux d'UNE connexion navigateur en `messages(occurrence)` pour
    `MediaBridgeFrameSource` (l'occurrence est portée par la session bot, pas par le socket)."""
    def _factory(_occurrence: ExternalMeetingOccurrence) -> AsyncIterator[dict]:
        return decode_bridge_frames(raw_stream)
    return _factory


async def serve_bot_bridge(host: str, port: int, on_connection: Any) -> Any:
    """Écoute les connexions du payload de capture (dep opt-in `websockets`). `on_connection`
    reçoit chaque connexion (une réunion). NON testé en CI → gate manuel."""
    import websockets  # dép opt-in

    async def _handler(conn: Any) -> None:
        await on_connection(conn)

    return await websockets.serve(_handler, host, port)

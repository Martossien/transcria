"""Vrai transport Zoom RTMS (L2) — orchestration des DEUX WebSockets, dep opt-in `websockets`.

Le protocole (handshake signaling → média, `client_ready`, keepalive bidirectionnel) est
piloté contre une abstraction de connexion INJECTÉE (`WsLike`) → **testable en CI** avec des
connexions factices ; l'ouverture réelle des WebSockets (`websockets.connect`, JSON) est thin
et confirmée au gate manuel. Les builders/parsers de messages vivent dans `rtms.py`.

Enchaînement (rtms-samples RTMS_CONNECTION_FLOW) : webhook `meeting.rtms_started` fournit
`server_urls` (signaling) → handshake signaling `1`→`2` (l'URL média est dans la réponse) →
connexion média → handshake média `3`→`4` → `client_ready 7` sur le SIGNALING → les paquets
audio `14` arrivent sur le MÉDIA. Keepalive `12`→`13` à honorer sur les deux, sinon coupure.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol

from connector_service.contract import ExternalMeetingOccurrence
from connector_service.live.rtms import (
    MEDIA_HANDSHAKE_RESP,
    SIGNALING_HANDSHAKE_RESP,
    client_ready,
    keepalive_response,
    media_handshake,
    signaling_handshake,
)


class RtmsError(RuntimeError):
    """Échec du handshake RTMS (status_code non nul, URL média absente…)."""


class WsLike(Protocol):
    """Connexion WebSocket RTMS abstraite (messages JSON). `recv_json` lève
    `StopAsyncIteration` à la fermeture (le thin adapter réel mappe `ConnectionClosed`)."""

    async def send_json(self, msg: dict) -> None: ...
    async def recv_json(self) -> dict: ...
    async def close(self) -> None: ...


ConnectMedia = Callable[[str], Awaitable[WsLike]]


def _status_ok(msg: dict) -> bool:
    return msg.get("status_code", 0) in (0, None)


def _extract_media_url(resp: dict) -> str:
    urls = (resp.get("media_server") or {}).get("server_urls")
    if isinstance(urls, dict):
        urls = urls.get("all")
    if isinstance(urls, list):
        urls = urls[0] if urls else None
    if not isinstance(urls, str) or not urls:
        raise RtmsError("URL du serveur média absente de la réponse de signaling")
    return urls


async def _recv_until(conn: WsLike, expected_type: int) -> dict:
    """Lit en répondant aux keepalive jusqu'à obtenir le type de message attendu. Une
    fermeture pendant la négociation (chemin d'échec NOMINAL : signature refusée, stream
    expiré) est convertie en `RtmsError` clair — sinon `StopAsyncIteration` traverse le
    générateur async et devient un `RuntimeError` obscur (PEP 479)."""
    while True:
        try:
            msg = await conn.recv_json()
        except StopAsyncIteration:
            raise RtmsError("connexion fermée pendant le handshake RTMS") from None
        ka = keepalive_response(msg)
        if ka is not None:
            await conn.send_json(ka)
            continue
        if msg.get("msg_type") == expected_type:
            return msg


async def rtms_handshake(signaling: WsLike, connect_media: ConnectMedia, *, client_id: str,
                         client_secret: str, meeting_uuid: str, rtms_stream_id: str,
                         sequence: int) -> WsLike:
    """Déroule le handshake complet et retourne la connexion MÉDIA prête à streamer."""
    await signaling.send_json(signaling_handshake(
        client_id, client_secret, meeting_uuid, rtms_stream_id, sequence))
    resp = await _recv_until(signaling, SIGNALING_HANDSHAKE_RESP)
    if not _status_ok(resp):
        raise RtmsError(f"handshake signaling refusé (status={resp.get('status_code')})")

    media = await connect_media(_extract_media_url(resp))
    await media.send_json(media_handshake(client_id, client_secret, meeting_uuid, rtms_stream_id))
    mresp = await _recv_until(media, MEDIA_HANDSHAKE_RESP)
    if not _status_ok(mresp):
        await media.close()
        raise RtmsError(f"handshake média refusé (status={mresp.get('status_code')})")

    await signaling.send_json(client_ready(rtms_stream_id))   # msg_type 7 sur le SIGNALING
    return media


async def keepalive_forever(conn: WsLike) -> None:
    """Répond indéfiniment aux keepalive d'un socket (jusqu'à fermeture/annulation). Utilisé
    en tâche de fond pour le socket signaling pendant que le média stream."""
    try:
        while True:
            msg = await conn.recv_json()
            ka = keepalive_response(msg)
            if ka is not None:
                await conn.send_json(ka)
    except (asyncio.CancelledError, StopAsyncIteration):
        return
    except Exception:  # noqa: BLE001 — le responder ne doit pas faire tomber la session
        return


async def rtms_audio_messages(signaling: WsLike, media: WsLike) -> AsyncIterator[dict]:
    """Yield les messages du socket MÉDIA (audio `14` inclus), en répondant aux keepalive sur
    les DEUX sockets — un responder signaling tourne en tâche de fond."""
    bg = asyncio.ensure_future(keepalive_forever(signaling))
    try:
        while True:
            try:
                msg = await media.recv_json()
            except StopAsyncIteration:
                return
            ka = keepalive_response(msg)
            if ka is not None:
                await media.send_json(ka)
                continue
            yield msg
    finally:
        bg.cancel()


def rtms_media_source(signaling: WsLike, connect_media: ConnectMedia, *, client_id: str,
                      client_secret: str, meeting_uuid: str, rtms_stream_id: str,
                      sequence: int = 0) -> Callable[
                          [ExternalMeetingOccurrence], AsyncIterator[dict]]:
    """`media_messages` prêt pour `RtmsMediaFrameSource` : handshake puis flux de messages
    média. Connexions INJECTÉES → testable ; l'ouverture réelle est thin (gate manuel)."""
    consumed = {"done": False}

    def _factory(_occurrence: ExternalMeetingOccurrence) -> AsyncIterator[dict]:
        async def _open() -> AsyncIterator[dict]:
            # Le signaling est consommé + fermé au 1er flux : refuser un 2e usage (retry /
            # nouvelle occurrence) plutôt que de re-handshaker un socket fermé (erreur obscure).
            if consumed["done"]:
                raise RtmsError("source RTMS déjà consommée — rouvrir le signaling")
            consumed["done"] = True
            media = await rtms_handshake(
                signaling, connect_media, client_id=client_id, client_secret=client_secret,
                meeting_uuid=meeting_uuid, rtms_stream_id=rtms_stream_id, sequence=sequence)
            try:
                async for msg in rtms_audio_messages(signaling, media):
                    yield msg
            finally:
                await media.close()
                await signaling.close()
        return _open()
    return _factory


# --------------------------------------------------------------------------- #
#  Thin adapter WebSocket réel (dep opt-in `websockets`) — gate manuel
# --------------------------------------------------------------------------- #
class _WebsocketsWs:
    """Adapte une connexion `websockets` en `WsLike` (JSON). Mappe la fermeture en
    `StopAsyncIteration`. Non testé en CI."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def send_json(self, msg: dict) -> None:
        import json
        await self._conn.send(json.dumps(msg))

    async def recv_json(self) -> dict:
        import json

        import websockets
        try:
            raw = await self._conn.recv()
        except websockets.exceptions.ConnectionClosed as exc:  # SEULE la fermeture = fin
            raise StopAsyncIteration from exc                  # (une vraie erreur doit remonter)
        return dict(json.loads(raw))

    async def close(self) -> None:
        await self._conn.close()


async def connect_ws(url: str) -> WsLike:
    """Ouvre une connexion WebSocket RTMS réelle (dep opt-in `websockets`). Gate manuel."""
    import websockets  # dép opt-in

    conn = await websockets.connect(url)
    return _WebsocketsWs(conn)

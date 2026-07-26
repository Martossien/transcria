"""Vrai transport STT WhisperLiveKit — WebSocket JSON `/asr`. dep OPT-IN `websockets`.

WhisperLiveKit est un **WebSocket** (pas du SSE) : le client pousse l'audio PCM et reçoit des
messages JSON (`{"type":"config"}` au début, puis `{"status":"active_transcription", lines,
buffer_transcription}`, `{"type":"ready_to_stop"}` à la fin). L'orchestration (pump audio /
recv JSON) est pilotée contre un socket et un décodeur INJECTÉS → **testable en CI** ; le
WebSocket réel est thin (gate manuel). Le parsing lines/buffer vit dans `wlk.py`.

⚠️ L'encodage d'entrée exact attendu par le serveur (PCM brut vs conteneur) est à confirmer au
gate — ici on pousse `AudioFrame.payload` tel quel (`pcm_s16le`), le point de branchement le
plus simple.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol

from connector_service.contract import AudioFrame
from connector_service.live._ws_drive import drive_recv

Decode = Callable[[bytes], dict]           # json.loads


class WsMixed(Protocol):
    """WebSocket : envoi binaire (PCM), réception texte JSON. `recv` lève `StopAsyncIteration`
    à la fermeture."""

    async def send(self, data: bytes) -> None: ...
    async def recv(self) -> bytes: ...
    async def close(self) -> None: ...


async def pump_pcm(ws: WsMixed, frames: AsyncIterator[AudioFrame]) -> None:
    """Pousse le PCM de chaque frame vers le serveur. Extrait pour test déterministe."""
    async for af in frames:
        await ws.send(af.payload)


def wlk_connect(open_ws: Callable[[], Awaitable[WsMixed]], *, decode: Decode
                ) -> Callable[[AsyncIterator[AudioFrame]], AsyncIterator[dict]]:
    """`connect(frames)` pour `wlk_open_stream` : pousse l'audio et yield les messages JSON
    décodés. Socket/décodeur INJECTÉS → testable ; l'implémentation réelle (websockets) est
    fournie par `wlk_ws_connect`."""
    def _factory(frames: AsyncIterator[AudioFrame]) -> AsyncIterator[dict]:
        async def _open() -> AsyncIterator[dict]:
            ws = await open_ws()
            async for event in drive_recv(ws, pump_pcm(ws, frames), decode):
                yield event
        return _open()
    return _factory


class _WebsocketsMixed:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def send(self, data: bytes) -> None:
        await self._conn.send(data)

    async def recv(self) -> bytes:
        import websockets
        try:
            raw = await self._conn.recv()
        except websockets.exceptions.ConnectionClosed as exc:  # SEULE la fermeture = fin
            raise StopAsyncIteration from exc
        return raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)

    async def close(self) -> None:
        await self._conn.close()


def wlk_ws_connect(url: str) -> Callable[[AsyncIterator[AudioFrame]], AsyncIterator[dict]]:
    """Transport WhisperLiveKit réel (dep opt-in `websockets`). Gate manuel."""
    import json

    async def _open_ws() -> WsMixed:
        import websockets  # dép opt-in
        conn = await websockets.connect(url)
        return _WebsocketsMixed(conn)

    def _decode(raw: bytes) -> dict:
        return dict(json.loads(raw))

    return wlk_connect(_open_ws, decode=_decode)

"""Vrai transport STT Kyutai / moshi — WebSocket msgpack `/api/asr-streaming`.

deps OPT-IN `websockets` + `msgpack`. L'orchestration (drainer l'audio → `Audio` msgpack ;
recevoir → événements décodés ; silence obligatoire avant/après + après le Marker) est
pilotée contre un socket et un codec INJECTÉS → **testable en CI** ; le msgpack et les
WebSockets réels sont thin (gate manuel). Le parsing des événements (Word/EndWord/Step/
Marker → committed/partial/final) vit dans `kyutai.py` (`KyutaiAccumulator`).

⚠️ Kyutai attend du **24 kHz float32 mono**. Nos `AudioFrame` sont `pcm_s16le` (16 kHz pour
RTMS/LiveKit). `pcm16_to_float32` convertit le format ; le RÉÉCHANTILLONNAGE vers 24 kHz est
un hook injecté (`resample`) — à 16 kHz→24 kHz on branche un resampler au gate (ou on crée
l'`AudioStream` LiveKit directement à 24 kHz). Sans hook, on suppose l'entrée déjà à 24 kHz.
"""
from __future__ import annotations

import struct
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol

from connector_service.contract import AudioFrame
from connector_service.live.kyutai import audio_message, marker_message

SILENCE_SAMPLES = 24000            # ~1 s de silence @24 kHz (quirk 2.6B : avant ET après)


def pcm16_to_float32(payload: bytes) -> list[float]:
    """PCM `s16le` → floats [-1, 1[. Ignore un octet final impair (frame tronquée)."""
    n = len(payload) // 2
    if n == 0:
        return []
    return [s / 32768.0 for s in struct.unpack(f"<{n}h", payload[:n * 2])]


class WsBytes(Protocol):
    """WebSocket binaire abstrait. `recv` lève `StopAsyncIteration` à la fermeture."""

    async def send(self, data: bytes) -> None: ...
    async def recv(self) -> bytes: ...
    async def close(self) -> None: ...


Encode = Callable[[dict], bytes]           # msgpack.packb(..., use_single_float=True)
Decode = Callable[[bytes], dict]           # msgpack.unpackb(...)
Resample = Callable[[list[float], int], list[float]]   # (samples, in_rate) -> 24 kHz


async def _send_silence(ws: WsBytes, encode: Encode, samples: int) -> None:
    if samples > 0:
        await ws.send(encode(audio_message([0.0] * samples)))


async def pump_audio(ws: WsBytes, frames: AsyncIterator[AudioFrame], *, encode: Encode,
                     resample: Resample | None = None,
                     silence_samples: int = SILENCE_SAMPLES) -> None:
    """Pousse le flux audio vers Kyutai : silence AVANT, chaque frame en `Audio`, silence
    APRÈS, `Marker`, puis silence (délai asr). Extrait pour être testable déterministe."""
    await _send_silence(ws, encode, silence_samples)
    async for af in frames:
        pcm = pcm16_to_float32(af.payload)
        if resample is not None and af.sample_rate_hz != 24000:
            pcm = resample(pcm, af.sample_rate_hz)
        await ws.send(encode(audio_message(pcm)))
    await _send_silence(ws, encode, silence_samples)
    await ws.send(encode(marker_message()))
    await _send_silence(ws, encode, silence_samples)


def kyutai_connect(open_ws: Callable[[], Awaitable[WsBytes]], *, encode: Encode, decode: Decode,
                   resample: Resample | None = None, silence_samples: int = SILENCE_SAMPLES
                   ) -> Callable[[AsyncIterator[AudioFrame]], AsyncIterator[dict]]:
    """`connect(frames)` pour `kyutai_open_stream` : ouvre le WS, pousse l'audio (silence
    avant/après + Marker), et yield les événements décodés. Socket/codec INJECTÉS → testable ;
    l'implémentation réelle (websockets + msgpack) est fournie par `kyutai_ws_connect`."""
    import asyncio

    def _factory(frames: AsyncIterator[AudioFrame]) -> AsyncIterator[dict]:
        async def _open() -> AsyncIterator[dict]:
            ws = await open_ws()
            pump = asyncio.ensure_future(pump_audio(
                ws, frames, encode=encode, resample=resample, silence_samples=silence_samples))
            try:
                while True:
                    try:
                        raw = await ws.recv()
                    except StopAsyncIteration:
                        return
                    yield decode(raw)
            finally:
                pump.cancel()
                await ws.close()

        return _open()
    return _factory


# --------------------------------------------------------------------------- #
#  Thin adapters réels (deps opt-in) — gate manuel
# --------------------------------------------------------------------------- #
class _WebsocketsBytes:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def send(self, data: bytes) -> None:
        await self._conn.send(data)

    async def recv(self) -> bytes:
        try:
            return bytes(await self._conn.recv())
        except Exception as exc:  # ConnectionClosed → fin
            raise StopAsyncIteration from exc

    async def close(self) -> None:
        await self._conn.close()


def kyutai_ws_connect(url: str, *, api_key: str | None = None, resample: Resample | None = None
                      ) -> Callable[[AsyncIterator[AudioFrame]], AsyncIterator[dict]]:
    """Transport Kyutai réel (deps opt-in `websockets` + `msgpack`). Gate manuel."""
    import msgpack  # dép opt-in

    async def _open_ws() -> WsBytes:
        import websockets  # dép opt-in
        headers = {"kyutai-api-key": api_key} if api_key else None
        conn = await websockets.connect(url, additional_headers=headers)
        return _WebsocketsBytes(conn)

    def _encode(msg: dict) -> bytes:
        return bytes(msgpack.packb(msg, use_single_float=True))

    def _decode(raw: bytes) -> dict:
        return dict(msgpack.unpackb(raw))

    return kyutai_connect(_open_ws, encode=_encode, decode=_decode, resample=resample)

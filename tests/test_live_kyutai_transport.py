"""L0 — transport STT Kyutai : conversion PCM, pump audio (silence/Marker), boucle recv."""
from __future__ import annotations

import asyncio
import struct

import pytest

from connector_service.contract import AudioFrame
from connector_service.live.kyutai_transport import (
    kyutai_connect,
    pcm16_to_float32,
    pump_audio,
)


def _af(payload, rate=24000):
    return AudioFrame(provider="visio", provider_account_id="a", external_occurrence_id="o",
                      track_id="t", sequence_number=0, media_timestamp_ms=0,
                      wall_clock_timestamp="2026-07-25T00:00:00Z", duration_ms=20,
                      encoding="pcm_s16le", sample_rate_hz=rate, channels=1,
                      sample_count=len(payload) // 2, payload=payload)


class FakeWs:
    def __init__(self, incoming=None):
        self._in = list(incoming or [])
        self.sent: list = []
        self.closed = False

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        if not self._in:
            raise StopAsyncIteration
        return self._in.pop(0)

    async def close(self):
        self.closed = True


async def _frames(items):
    for it in items:
        yield it


def test_pcm16_to_float32():
    payload = struct.pack("<4h", 0, 16384, -32768, 32767)
    out = pcm16_to_float32(payload)
    assert out[0] == 0.0 and out[1] == 0.5 and out[2] == -1.0
    assert abs(out[3] - 0.99997) < 1e-4
    assert pcm16_to_float32(b"\x01") == []                     # octet impair → vide


def test_pump_audio_silence_avant_apres_et_marker():
    ws = FakeWs()
    pcm = struct.pack("<2h", 16384, -16384)
    asyncio.run(pump_audio(ws, _frames([_af(pcm)]), encode=lambda d: d, silence_samples=2))
    types = [m["type"] for m in ws.sent]
    assert types == ["Audio", "Audio", "Audio", "Marker", "Audio"]   # sil, data, sil, marker, sil
    assert ws.sent[0]["pcm"] == [0.0, 0.0]                     # silence avant
    assert ws.sent[1]["pcm"] == [0.5, -0.5]                    # la frame convertie
    assert ws.sent[3] == {"type": "Marker", "id": 0}


def test_pump_audio_resample_si_pas_24k():
    ws = FakeWs()
    seen = {}

    def resample(samples, rate):
        seen["rate"] = rate
        return samples + samples                                # marqueur : double la liste

    pcm = struct.pack("<1h", 16384)
    asyncio.run(pump_audio(ws, _frames([_af(pcm, rate=16000)]), encode=lambda d: d,
                           resample=resample, silence_samples=0))
    assert seen["rate"] == 16000                                # resample appelé pour 16k
    assert ws.sent[0]["pcm"] == [0.5, 0.5]                      # sortie du resampler


class BlockingWs:
    """`recv` BLOQUE jusqu'à `close()` — simule un serveur qui n'émet plus (le vrai piège :
    sans le correctif, la boucle recv gèle à jamais si le pump meurt)."""

    def __init__(self):
        self._closed = asyncio.Event()
        self.closed = False

    async def send(self, data):
        pass

    async def recv(self):
        await self._closed.wait()
        raise StopAsyncIteration

    async def close(self):
        self.closed = True
        self._closed.set()


def test_pump_exception_ferme_le_socket_et_remonte():
    """Régression B2 : le provider (frames) lève SANS casser le socket → recv gèlerait. Le
    correctif ferme le socket (débloque recv) et RE-PROPAGE l'exception du pump."""
    async def boom_frames():
        raise RuntimeError("provider mort")
        yield  # pragma: no cover

    ws = BlockingWs()

    async def open_ws():
        return ws

    connect = kyutai_connect(open_ws, encode=lambda d: d, decode=lambda x: x, silence_samples=0)

    async def _run():
        return [e async for e in connect(boom_frames())]

    with pytest.raises(RuntimeError, match="provider mort"):
        asyncio.run(_run())
    assert ws.closed                                              # socket fermé → recv débloqué


def test_kyutai_connect_boucle_recv_decode():
    ws = FakeWs(incoming=[{"type": "Ready"}, {"type": "Word", "text": "hi", "start_time": 0.0}])

    async def open_ws():
        return ws

    connect = kyutai_connect(open_ws, encode=lambda d: d, decode=lambda x: x, silence_samples=0)

    async def _collect():
        return [e async for e in connect(_frames([]))]

    events = asyncio.run(_collect())
    assert events == [{"type": "Ready"}, {"type": "Word", "text": "hi", "start_time": 0.0}]
    assert ws.closed

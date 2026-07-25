"""L0 — transport STT WhisperLiveKit : pump PCM + boucle recv JSON, jusqu'aux Hypothesis."""
from __future__ import annotations

import asyncio

from connector_service.contract import AudioFrame, ExternalMeetingOccurrence
from connector_service.live.engines import StreamingTranscriber
from connector_service.live.session import LiveSession
from connector_service.live.wlk import wlk_open_stream
from connector_service.live.wlk_transport import pump_pcm, wlk_connect

OCC = ExternalMeetingOccurrence(provider="visio", provider_account_id="a",
                                external_occurrence_id="occ-1")


def _af(payload):
    return AudioFrame(provider="visio", provider_account_id="a", external_occurrence_id="occ-1",
                      track_id="t", sequence_number=0, media_timestamp_ms=0,
                      wall_clock_timestamp="2026-07-25T00:00:00Z", duration_ms=20,
                      encoding="pcm_s16le", sample_rate_hz=16000, channels=1,
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


class _Provider:
    def __init__(self, payloads):
        self._payloads = payloads

    async def stream_audio(self, _occurrence):
        for p in self._payloads:
            yield _af(p)


def test_pump_pcm_envoie_le_payload_brut():
    ws = FakeWs()
    asyncio.run(pump_pcm(ws, _frames([_af(b"\x01\x02"), _af(b"\x03\x04")])))
    assert ws.sent == [b"\x01\x02", b"\x03\x04"]


def test_wlk_connect_bout_en_bout_lines_et_buffer():
    msgs = [
        {"type": "config"},
        {"status": "active_transcription",
         "lines": [{"text": "bonjour le", "start": 0.0, "end": 1.0}],
         "buffer_transcription": "monde"},
        {"status": "active_transcription",
         "lines": [{"text": "bonjour le", "start": 0.0, "end": 1.0},
                   {"text": "tout va bien", "start": 1.0, "end": 2.0}],
         "buffer_transcription": ""},
    ]
    ws = FakeWs(incoming=msgs)

    async def open_ws():
        return ws

    # decode identité (les messages factices sont déjà des dicts).
    connect = wlk_connect(open_ws, decode=lambda x: x)
    transcriber = StreamingTranscriber(wlk_open_stream(connect), uses_local_agreement=False)

    partials, finals = [], []
    session = LiveSession(transcriber, on_partial=lambda s: partials.append(s.text),
                          on_final=lambda s: finals.append(s.text))
    out = asyncio.run(session.run(_Provider([b"\x00\x00"]), OCC))
    assert [s.text for s in out] == ["bonjour le", "tout va bien"]
    assert partials == ["monde"]
    assert ws.closed

"""L2 — orchestration Zoom RTMS : handshake 2-WS, keepalive, client_ready (connexions factices)."""
from __future__ import annotations

import asyncio
import base64

import pytest

from connector_service.contract import ExternalMeetingOccurrence
from connector_service.live.media import zoom_rtms_provider
from connector_service.live.rtms import DATA_AUDIO, RtmsMediaFrameSource
from connector_service.live.rtms_transport import (
    RtmsError,
    keepalive_forever,
    rtms_handshake,
    rtms_media_source,
)

PARAMS = dict(client_id="CID", client_secret="SECRET", meeting_uuid="mtg-1",
              rtms_stream_id="stream-9", sequence=7)
OCC = ExternalMeetingOccurrence(provider="zoom", provider_account_id="a",
                                external_occurrence_id="mtg-1")


class FakeWs:
    def __init__(self, incoming=None):
        self._in = list(incoming or [])
        self.sent: list = []
        self.closed = False

    def feed(self, *msgs):
        self._in.extend(msgs)

    async def send_json(self, msg):
        self.sent.append(msg)

    async def recv_json(self):
        if not self._in:
            raise StopAsyncIteration
        return self._in.pop(0)

    async def close(self):
        self.closed = True


def _sig_resp(status=0, media_url="wss://media.zoom/rtms"):
    return {"msg_type": 2, "status_code": status,
            "media_server": {"server_urls": {"all": [media_url]}}}


def _audio(uid, pcm, ts):
    return {"msg_type": DATA_AUDIO,
            "content": {"user_id": uid, "user_name": f"u{uid}",
                        "data": base64.b64encode(pcm).decode("ascii"), "timestamp": ts}}


def test_handshake_complet_ordre_des_messages():
    signaling = FakeWs([_sig_resp()])
    media = FakeWs([{"msg_type": 4, "status_code": 0}])

    async def connect_media(url):
        assert url == "wss://media.zoom/rtms"
        return media

    async def run():
        return await rtms_handshake(signaling, connect_media, **PARAMS)

    result = asyncio.run(run())
    assert result is media
    assert [m["msg_type"] for m in signaling.sent] == [1, 7]     # handshake puis client_ready
    assert signaling.sent[0]["sequence"] == 7 and "signature" in signaling.sent[0]
    assert media.sent[0]["msg_type"] == 3                         # handshake média
    assert media.sent[0]["media_params"]["audio"]["data_opt"] == 2


def test_handshake_repond_aux_keepalive_pendant_la_negociation():
    signaling = FakeWs([{"msg_type": 12, "timestamp": 5}, _sig_resp()])
    media = FakeWs([{"msg_type": 4, "status_code": 0}])

    async def connect_media(_url):
        return media

    asyncio.run(rtms_handshake(signaling, connect_media, **PARAMS))
    assert {"msg_type": 13, "timestamp": 5} in signaling.sent   # keepalive honoré


def test_handshake_refuse_leve():
    signaling = FakeWs([_sig_resp(status=1)])

    async def connect_media(_url):
        return FakeWs()

    with pytest.raises(RtmsError, match="signaling"):
        asyncio.run(rtms_handshake(signaling, connect_media, **PARAMS))


def test_keepalive_forever_repond_puis_termine():
    conn = FakeWs([{"msg_type": 12, "timestamp": 1}, {"msg_type": 14}])  # puis épuisé
    asyncio.run(keepalive_forever(conn))
    assert conn.sent == [{"msg_type": 13, "timestamp": 1}]      # 12→13, le 14 ignoré, fin propre


def test_media_source_bout_en_bout_vers_frames():
    signaling = FakeWs([_sig_resp()])
    media = FakeWs([{"msg_type": 4, "status_code": 0},          # réponse handshake média
                    {"msg_type": 12, "timestamp": 9},           # keepalive → répondu
                    _audio(1, b"\x00\x00" * 160, 1000),
                    _audio(1, b"\x00\x00" * 160, 1020)])

    async def connect_media(_url):
        return media

    source = rtms_media_source(signaling, connect_media, **PARAMS)
    provider = zoom_rtms_provider(RtmsMediaFrameSource(source,
                                                       now=lambda: "2026-07-25T10:00:00+00:00"))

    async def _collect():
        return [f async for f in provider.stream_audio(OCC)]

    frames = asyncio.run(_collect())
    assert [(f.participant_id, f.sequence_number) for f in frames] == [("1", 0), ("1", 1)]
    assert frames[0].provider == "zoom" and frames[0].sample_rate_hz == 16000
    assert {"msg_type": 13, "timestamp": 9} in media.sent       # keepalive média honoré
    assert media.closed and signaling.closed                    # fermeture propre

"""L2 — transport Zoom RTMS : handshake, keepalive, parse audio nichés, frame_source."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac

from connector_service.contract import ExternalMeetingOccurrence
from connector_service.live.media import zoom_rtms_provider
from connector_service.live.rtms import (
    AUDIO_MEDIA_PARAMS,
    DATA_AUDIO,
    RtmsMediaFrameSource,
    client_ready,
    keepalive_response,
    media_handshake,
    parse_audio_frame,
    signaling_handshake,
)
from connector_service.signatures import rtms_handshake_signature

OCC = ExternalMeetingOccurrence(provider="zoom", provider_account_id="acct",
                                external_occurrence_id="mtg-uuid-1")


def _audio_msg(user_id, name, pcm, ts):
    return {"msg_type": DATA_AUDIO,
            "content": {"user_id": user_id, "user_name": name,
                        "data": base64.b64encode(pcm).decode("ascii"), "timestamp": ts}}


def test_handshake_signature_exacte():
    sig = rtms_handshake_signature("CID", "SECRET", "mtg-uuid-1", "stream-9")
    expected = hmac.new(b"SECRET", b"CID,mtg-uuid-1,stream-9", hashlib.sha256).hexdigest()
    assert sig == expected


def test_signaling_handshake_structure():
    msg = signaling_handshake("CID", "SECRET", "mtg-uuid-1", "stream-9", sequence=42)
    assert msg["msg_type"] == 1 and msg["buffer_data"] is False
    assert msg["meeting_uuid"] == "mtg-uuid-1" and msg["rtms_stream_id"] == "stream-9"
    assert msg["sequence"] == 42
    assert msg["signature"] == rtms_handshake_signature("CID", "SECRET", "mtg-uuid-1", "stream-9")


def test_media_handshake_negocie_multi_flux_16k():
    msg = media_handshake("CID", "SECRET", "mtg-uuid-1", "stream-9")
    assert msg["msg_type"] == 3 and msg["payload_encryption"] is False
    audio = msg["media_params"]["audio"]
    assert audio["codec"] == 1 and audio["sample_rate"] == 1 and audio["channel"] == 1
    assert audio["data_opt"] == 2                       # un flux par participant (≠ MIXED)
    assert AUDIO_MEDIA_PARAMS["data_opt"] == 2          # constante non mutée


def test_client_ready_et_keepalive():
    assert client_ready("stream-9") == {"msg_type": 7, "rtms_stream_id": "stream-9"}
    assert keepalive_response({"msg_type": 12, "timestamp": 777}) == {"msg_type": 13,
                                                                      "timestamp": 777}
    assert keepalive_response({"msg_type": 14}) is None
    assert keepalive_response("nope") is None


def test_parse_audio_frame_format_niche():
    pcm = b"\x01\x02" * 160                              # 320 octets = 160 échantillons
    frame = parse_audio_frame(_audio_msg(1234, "Alice", pcm, 5000),
                              sequence_number=7, wall_clock_timestamp="2026-07-25T00:00:05+00:00")
    assert frame is not None
    assert frame.participant_id == "1234"               # int coercé en str
    assert frame.participant_name == "Alice"
    assert frame.payload == pcm and frame.sequence_number == 7
    assert frame.media_timestamp_ms == 5000 and frame.sample_rate_hz == 16000
    assert frame.encoding == "pcm_s16le" and frame.channels == 1


def test_parse_audio_frame_rejette_non_audio():
    assert parse_audio_frame({"msg_type": 17}, sequence_number=0, wall_clock_timestamp="t") is None
    assert parse_audio_frame({"msg_type": 14, "content": {}}, sequence_number=0,
                             wall_clock_timestamp="t") is None


def test_parse_audio_frame_donnees_corrompues_retourne_none():
    """Régression B8 : un paquet malformé ne doit PAS tuer la session (contrat « None »)."""
    # base64 mal formé
    assert parse_audio_frame({"msg_type": DATA_AUDIO, "content": {"user_id": 1, "data": "AAA"}},
                             sequence_number=0, wall_clock_timestamp="t") is None
    # timestamp non entier
    good = base64.b64encode(b"\x00\x00").decode("ascii")
    assert parse_audio_frame(
        {"msg_type": DATA_AUDIO, "content": {"user_id": 1, "data": good, "timestamp": "nan"}},
        sequence_number=0, wall_clock_timestamp="t") is None


def _source(messages):
    async def _open(_occurrence):
        for m in messages:
            yield m
    return _open


def test_frame_source_seq_par_participant_et_keepalive():
    answered: list = []
    messages = [
        {"msg_type": 12, "timestamp": 111},             # keepalive → répondu, pas de frame
        _audio_msg(1, "A", b"\x00\x00" * 80, 1000),
        _audio_msg(2, "B", b"\x00\x00" * 80, 1000),
        _audio_msg(1, "A", b"\x00\x00" * 80, 1020),     # 2e frame de p1
    ]

    async def on_keepalive(resp):
        answered.append(resp)

    src = RtmsMediaFrameSource(_source(messages), on_keepalive=on_keepalive,
                               now=lambda: "2026-07-25T10:00:00+00:00")

    async def _collect():
        return [f async for f in src.frames(OCC)]

    frames = asyncio.run(_collect())
    assert answered == [{"msg_type": 13, "timestamp": 111}]
    assert [(f.participant_id, f.sequence_number) for f in frames] == [
        ("1", 0), ("2", 0), ("1", 1)]                   # compteur synthétisé PAR participant
    assert frames[0].media_timestamp_ms == 1000                      # epoch RTMS conservé
    assert frames[0].wall_clock_timestamp == "2026-07-25T10:00:00+00:00"  # heure d'arrivée


def test_integration_rtms_provider_produit_audioframe_zoom():
    src = RtmsMediaFrameSource(_source([_audio_msg(9, "Bob", b"\x00\x00" * 160, 2000)]))
    provider = zoom_rtms_provider(src)

    async def _collect():
        return [f async for f in provider.stream_audio(OCC)]

    frames = asyncio.run(_collect())
    assert frames[0].provider == "zoom" and frames[0].participant_id == "9"
    assert frames[0].sample_count == 160 and frames[0].duration_ms == 10
    assert frames[0].participant_display_name == "Bob"

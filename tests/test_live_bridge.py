"""C1/pont — MediaBridgeFrameSource : contrat PCM neutre (sidecar Teams .NET, bot…) → RawFrame."""
from __future__ import annotations

import asyncio
import base64

from connector_service.contract import ExternalMeetingOccurrence
from connector_service.live.bridge_source import MediaBridgeFrameSource, parse_bridge_message
from connector_service.live.media import teams_rtm_provider

OCC = ExternalMeetingOccurrence(provider="teams", provider_account_id="tenant",
                                external_occurrence_id="call-1")


def _msg(pid, pcm, name="", rate=16000, ts=0):
    return {"participant_id": pid, "participant_name": name,
            "pcm": base64.b64encode(pcm).decode("ascii"), "sample_rate_hz": rate,
            "channels": 1, "media_timestamp_ms": ts}


def test_parse_bridge_message_b64_et_bytes():
    parsed = parse_bridge_message(_msg("u1", b"\x01\x02" * 80, name="Alice", ts=500))
    pid, payload, rate, channels, name, media_ts = parsed
    assert pid == "u1" and payload == b"\x01\x02" * 80 and rate == 16000
    assert channels == 1 and name == "Alice" and media_ts == 500
    # accepte aussi des bytes bruts + rejette les messages sans PCM
    assert parse_bridge_message({"participant_id": "u", "pcm": b"\x00\x00"})[1] == b"\x00\x00"
    assert parse_bridge_message({"participant_id": "u"}) is None
    assert parse_bridge_message("nope") is None


def _source(messages):
    async def _open(_occurrence):
        for m in messages:
            yield m
    return _open


def test_bridge_frame_source_seq_par_participant():
    messages = [_msg("a", b"\x00\x00" * 80, name="A"),
                _msg("b", b"\x00\x00" * 80, name="B"),
                _msg("a", b"\x00\x00" * 80)]
    src = MediaBridgeFrameSource(_source(messages), now=lambda: "2026-07-25T10:00:00+00:00")

    async def _collect():
        return [f async for f in src.frames(OCC)]

    frames = asyncio.run(_collect())
    assert [(f.participant_id, f.sequence_number) for f in frames] == [
        ("a", 0), ("b", 0), ("a", 1)]                         # compteur synthétisé/participant
    assert frames[0].participant_name == "A"
    assert frames[0].wall_clock_timestamp == "2026-07-25T10:00:00+00:00"


def test_bridge_alimente_le_provider_teams():
    src = MediaBridgeFrameSource(_source([_msg("u9", b"\x00\x00" * 160, name="Carol")]))
    provider = teams_rtm_provider(src)

    async def _collect():
        return [f async for f in provider.stream_audio(OCC)]

    out = asyncio.run(_collect())
    assert out[0].provider == "teams" and out[0].participant_id == "u9"
    assert out[0].sample_count == 160 and out[0].participant_display_name == "Carol"

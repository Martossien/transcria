"""Bot — décodage du pont : messages WS bruts du payload JS → dicts → RawFrame."""
from __future__ import annotations

import asyncio
import base64
import json

from connector_service.bot.bridge_server import connection_messages, decode_bridge_frames
from connector_service.contract import ExternalMeetingOccurrence
from connector_service.live.bridge_source import MediaBridgeFrameSource

OCC = ExternalMeetingOccurrence(provider="bot", provider_account_id="a",
                                external_occurrence_id="jitsi-salle")


async def _raw(items):
    for it in items:
        yield it


def _pcm_msg(pid, pcm):
    return json.dumps({"participant_id": pid, "pcm": base64.b64encode(pcm).decode("ascii"),
                       "sample_rate_hz": 16000, "channels": 1})


def test_decode_ignore_le_json_illisible():
    async def _collect():
        return [m async for m in decode_bridge_frames(
            _raw([_pcm_msg("p1", b"\x00\x00"), "pas du json {", '["liste"]',
                  _pcm_msg("p2", b"\x01\x01")]))]

    out = asyncio.run(_collect())
    assert [m["participant_id"] for m in out] == ["p1", "p2"]      # illisible + non-dict écartés


def test_connection_messages_vers_frame_source():
    raw = _raw([_pcm_msg("alice", b"\x00\x00" * 80), _pcm_msg("alice", b"\x00\x00" * 80),
                _pcm_msg("bob", b"\x00\x00" * 80)])
    source = MediaBridgeFrameSource(connection_messages(raw),
                                    now=lambda: "2026-07-25T10:00:00+00:00")

    async def _collect():
        return [f async for f in source.frames(OCC)]

    frames = asyncio.run(_collect())
    assert [(f.participant_id, f.sequence_number) for f in frames] == [
        ("alice", 0), ("alice", 1), ("bob", 0)]                    # PCM du navigateur → RawFrame
    assert frames[0].sample_rate_hz == 16000

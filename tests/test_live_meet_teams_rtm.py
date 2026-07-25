"""R1/C1 — transports Meet Media (WebRTC 48 k, démux CSRC) et Teams RTM (dernier recours)."""
from __future__ import annotations

import asyncio

from connector_service.contract import ExternalMeetingOccurrence
from connector_service.live.media import meet_media_provider, teams_rtm_provider
from connector_service.live.meet_media import (
    MEET_MAX_STREAMS,
    MEET_SAMPLE_RATE_HZ,
    MeetMediaFrameSource,
    meet_demuxed_frame,
)
from connector_service.live.teams_rtm import TeamsRtmFrame, TeamsRtmFrameSource

MEET_OCC = ExternalMeetingOccurrence(provider="meet", provider_account_id="spaces/s",
                                     external_occurrence_id="conf-1")
TEAMS_OCC = ExternalMeetingOccurrence(provider="teams", provider_account_id="tenant",
                                      external_occurrence_id="call-1")


def _source(frames):
    async def _open(_occurrence):
        for f in frames:
            yield f
    return _open


def test_meet_demux_csrc_vers_participant_48k():
    mapping = {111: ("alice@corp", "Alice"), 222: ("bob@corp", "Bob")}
    known = meet_demuxed_frame(111, b"\x00\x00" * 480, mapping)
    unknown = meet_demuxed_frame(999, b"\x00\x00" * 480, mapping)
    assert known.participant_id == "alice@corp" and known.participant_name == "Alice"
    assert known.sample_rate_hz == MEET_SAMPLE_RATE_HZ == 48000
    assert unknown.participant_id == "csrc-999"          # CSRC inconnu → repli, frame conservée
    assert MEET_MAX_STREAMS == 3


def test_meet_media_provider_produit_audioframe_48k():
    frames = [meet_demuxed_frame(111, b"\x00\x00" * 480, {111: ("alice@corp", "Alice")})]
    provider = meet_media_provider(MeetMediaFrameSource(_source(frames),
                                                        now=lambda: "2026-07-25T10:00:00+00:00"))

    async def _collect():
        return [f async for f in provider.stream_audio(MEET_OCC)]

    out = asyncio.run(_collect())
    assert out[0].provider == "meet" and out[0].participant_id == "alice@corp"
    assert out[0].sample_rate_hz == 48000
    assert out[0].sample_count == 480 and out[0].duration_ms == 10   # 480 éch @48k = 10 ms
    assert out[0].wall_clock_timestamp == "2026-07-25T10:00:00+00:00"


def test_teams_rtm_provider_produit_audioframe_16k():
    frames = [TeamsRtmFrame(participant_id="u1", payload=b"\x00\x00" * 160,
                            participant_name="Carol")]
    provider = teams_rtm_provider(TeamsRtmFrameSource(_source(frames)))

    async def _collect():
        return [f async for f in provider.stream_audio(TEAMS_OCC)]

    out = asyncio.run(_collect())
    assert out[0].provider == "teams" and out[0].participant_id == "u1"
    assert out[0].sample_count == 160 and out[0].duration_ms == 10   # 160 éch @16k = 10 ms
    assert out[0].participant_display_name == "Carol"

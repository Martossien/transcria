"""L1/L2 — LiveAudioProvider (Visio/Zoom RTMS) : RawFrame → AudioFrame + intégration session."""
from __future__ import annotations

import asyncio

from connector_service.contract import ExternalMeetingOccurrence
from connector_service.live.agreement import Word
from connector_service.live.media import (
    RawFrame,
    visio_live_provider,
    zoom_rtms_provider,
)
from connector_service.live.session import Hypothesis, LiveSession

OCC = ExternalMeetingOccurrence(provider="visio", provider_account_id="acct",
                                external_occurrence_id="occ-1")


class _FakeSource:
    def __init__(self, frames):
        self._frames = frames

    async def frames(self, occurrence):
        for f in self._frames:
            yield f


async def _collect(agen):
    return [x async for x in agen]


def test_visio_live_convertit_raw_en_audioframe():
    raw = RawFrame(participant_id="p1", payload=b"\x00\x00" * 160, sequence_number=3,
                   media_timestamp_ms=60, wall_clock_timestamp="2026-07-25T10:00:00Z",
                   participant_name="Alice")
    frames = asyncio.run(_collect(visio_live_provider(_FakeSource([raw])).stream_audio(OCC)))
    af = frames[0]
    assert af.provider == "visio" and af.participant_id == "p1"
    assert af.participant_display_name == "Alice" and af.track_id == "track-p1"
    assert af.sample_count == 160 and af.duration_ms == 10       # 320 octets PCM16 @16k
    assert af.wall_clock_timestamp == "2026-07-25T10:00:00Z"


def test_zoom_rtms_provider_tag_provider():
    raw = RawFrame(participant_id="u9", payload=b"\x00\x00", sequence_number=0,
                   media_timestamp_ms=0, wall_clock_timestamp="2026-07-25T10:00:00Z")
    frames = asyncio.run(_collect(zoom_rtms_provider(_FakeSource([raw]))
                         .stream_audio(ExternalMeetingOccurrence(
                             provider="zoom", provider_account_id="a", external_occurrence_id="o"))))
    assert frames[0].provider == "zoom" and frames[0].participant_id == "u9"


class _RecordingTranscriber:
    uses_local_agreement = False

    def __init__(self):
        self.seen: list = []

    async def stream(self, frames):
        async for f in frames:
            self.seen.append(f.participant_id)
        yield Hypothesis([Word(t, i, i + 1) for i, t in enumerate(["bonjour", "le", "monde"])],
                         is_final=True)


def test_integration_provider_transcriber_session():
    raws = [RawFrame(participant_id=pid, payload=b"\x00\x00", sequence_number=i,
                     media_timestamp_ms=i * 20, wall_clock_timestamp="2026-07-25T10:00:00Z")
            for i, pid in enumerate(["p1", "p2", "p1"])]
    transcriber = _RecordingTranscriber()
    finals = asyncio.run(LiveSession(transcriber).run(visio_live_provider(_FakeSource(raws)), OCC))
    assert transcriber.seen == ["p1", "p2", "p1"]               # le transcriber a vu les frames
    assert [s.text for s in finals] == ["bonjour le monde"]
    assert finals[0].provenance == "final_live"

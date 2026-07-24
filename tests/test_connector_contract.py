"""A0 lot 2 — contrat providers par capacités (ADR-001 D3/D4).

Tests PURS (asyncio.run, pas de plugin) : prouvent qu'un provider n'expose que les
interfaces qu'il déclare, et que les flux contrôle/données ont la bonne forme.
"""
from __future__ import annotations

import asyncio

from connector_service.contract import (
    ArtifactProvider,
    LiveMediaProvider,
    ParticipantProvider,
    PlatformTranscriptProvider,
    ProviderCapabilities,
)
from connector_service.contract import ExternalMeetingOccurrence as Occ
from connector_service.fakes import (
    FakeFullProvider,
    FakeLiveProvider,
    FakePostMeetingProvider,
)

OCC = Occ(provider="visio", provider_account_id="acct", external_occurrence_id="occ-1")


def _run(coro):
    return asyncio.run(coro)


async def _collect(agen) -> list:
    return [frame async for frame in agen]


def test_capabilities_defaut_tout_false():
    c = ProviderCapabilities()
    assert not (c.post_meeting_recording or c.post_meeting_transcript or c.live_audio
                or c.live_transcript or c.participant_identity or c.separate_tracks)


def test_post_meeting_conforme_mais_pas_live():
    p = FakePostMeetingProvider()
    assert isinstance(p, ArtifactProvider)
    assert isinstance(p, ParticipantProvider)
    assert isinstance(p, PlatformTranscriptProvider)
    assert not isinstance(p, LiveMediaProvider)         # pas de stream_audio
    assert p.capabilities.post_meeting_recording and not p.capabilities.live_audio
    arts = _run(p.fetch_artifacts(OCC))
    assert arts and arts[0].artifact_type == "recording"
    # Transcript plateforme = AUXILIAIRE (existe, mais jamais canonique par défaut).
    trs = _run(p.fetch_platform_transcripts(OCC))
    assert trs and trs[0].language == "fr"


def test_live_conforme_mais_pas_post():
    p = FakeLiveProvider()
    assert isinstance(p, LiveMediaProvider)
    assert not isinstance(p, ArtifactProvider)          # pas de fetch_artifacts
    assert p.capabilities.live_audio and p.capabilities.separate_tracks
    frames = _run(_collect(p.stream_audio(OCC, frames=3)))
    assert [f.sequence_number for f in frames] == [0, 1, 2]
    assert frames[0].media_timestamp_ms == 0 and frames[0].sample_rate_hz == 16000
    assert frames[0].participant_id == "p1"


def test_full_conforme_a_toutes_les_interfaces():
    p = FakeFullProvider()
    for proto in (ArtifactProvider, ParticipantProvider,
                  PlatformTranscriptProvider, LiveMediaProvider):
        assert isinstance(p, proto)
    assert p.capabilities.live_audio and p.capabilities.post_meeting_recording


def test_audioframe_timestamps_desambigus():
    frame = _run(_collect(FakeLiveProvider().stream_audio(OCC, frames=1)))[0]
    # media (position réunion) vs wall_clock (UTC) explicites ; plus de start_timestamp ambigu.
    assert hasattr(frame, "media_timestamp_ms") and hasattr(frame, "wall_clock_timestamp")
    assert not hasattr(frame, "start_timestamp")

"""L1 — transport Visio/LiveKit : mapping LiveKitFrame → RawFrame (séquence/horloge synthé)."""
from __future__ import annotations

import asyncio

from connector_service.contract import ExternalMeetingOccurrence
from connector_service.live.livekit_source import LiveKitFrame, LiveKitFrameSource
from connector_service.live.media import visio_live_provider

OCC = ExternalMeetingOccurrence(provider="visio", provider_account_id="acct",
                                external_occurrence_id="room-42")


def _source(frames):
    async def _open(_occurrence):
        for f in frames:
            yield f
    return _open


def test_mapping_seq_et_horloge_media_par_participant():
    # 160 échantillons @16k = 10 ms ; l'horloge média cumule PAR participant.
    pcm = b"\x00\x00" * 160
    frames = [LiveKitFrame(participant_id="alice", payload=pcm, participant_name="Alice",
                           track_id="TR1"),
              LiveKitFrame(participant_id="bob", payload=pcm, participant_name="Bob"),
              LiveKitFrame(participant_id="alice", payload=pcm)]
    src = LiveKitFrameSource(_source(frames), now=lambda: "2026-07-25T10:00:00+00:00")

    async def _collect():
        return [f async for f in src.frames(OCC)]

    out = asyncio.run(_collect())
    assert [(f.participant_id, f.sequence_number, f.media_timestamp_ms) for f in out] == [
        ("alice", 0, 0), ("bob", 0, 0), ("alice", 1, 10)]     # alice avance à 10 ms, bob à 0
    assert out[0].track_id == "TR1" and out[1].track_id == "track-bob"   # sid ou repli
    assert out[0].participant_name == "Alice"
    assert out[0].wall_clock_timestamp == "2026-07-25T10:00:00+00:00"    # heure d'arrivée
    assert out[0].sample_rate_hz == 16000 and out[0].channels == 1


def test_integration_visio_provider_produit_audioframe():
    src = LiveKitFrameSource(_source([LiveKitFrame(participant_id="p1", payload=b"\x00\x00" * 160,
                                                   participant_name="Alice")]))
    provider = visio_live_provider(src)

    async def _collect():
        return [f async for f in provider.stream_audio(OCC)]

    out = asyncio.run(_collect())
    assert out[0].provider == "visio" and out[0].participant_id == "p1"
    assert out[0].sample_count == 160 and out[0].duration_ms == 10
    assert out[0].participant_display_name == "Alice"

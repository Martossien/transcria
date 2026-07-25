"""L0 — client STT streaming générique (parse_event + StreamingTranscriber + SSE)."""
from __future__ import annotations

import asyncio

from connector_service.contract import AudioFrame, ExternalMeetingOccurrence
from connector_service.live.engines import (
    StreamingTranscriber,
    parse_event,
    sse_line_events,
)
from connector_service.live.session import LiveSession

OCC = ExternalMeetingOccurrence(provider="visio", provider_account_id="a",
                                external_occurrence_id="occ-1")


def test_parse_event_words_et_final():
    hyp = parse_event({"words": [{"text": "bonjour", "start": 0.0, "end": 0.5},
                                 {"text": "monde", "start": 0.5, "end": 1.0}], "final": True})
    assert [w.text for w in hyp.partial] == ["bonjour", "monde"] and hyp.is_final is True
    assert hyp.partial[0].start == 0.0 and not hyp.committed


def test_parse_event_texte_seul():
    hyp = parse_event({"text": "salut tout le monde"})
    assert [w.text for w in hyp.partial] == ["salut", "tout", "le", "monde"]
    assert hyp.is_final is False


def test_parse_event_committed_partial_split():
    """Format réel des serveurs natifs : préfixe stable + queue instable séparés."""
    hyp = parse_event({"committed": [{"text": "bonjour", "start": 0.0, "end": 0.5}],
                       "partial": [{"text": "le", "start": 0.5, "end": 0.7}], "final": False})
    assert [w.text for w in hyp.committed] == ["bonjour"]
    assert [w.text for w in hyp.partial] == ["le"] and hyp.is_final is False


class _FakeProvider:
    async def stream_audio(self, occurrence):
        yield AudioFrame(provider="visio", provider_account_id="a", external_occurrence_id="occ-1",
                         track_id="t", sequence_number=0, media_timestamp_ms=0,
                         wall_clock_timestamp="2026-07-25T00:00:00Z", duration_ms=20,
                         encoding="pcm_s16le", sample_rate_hz=16000, channels=1,
                         sample_count=320, payload=b"\x00")


def _fake_open(events):
    async def _open(frames):
        async for _ in frames:
            pass
        for e in events:
            yield e
    return _open


def test_streaming_transcriber_vers_session():
    events = [{"text": "hi"}, {"words": [{"text": "hi", "start": 0}, {"text": "there", "start": 1}],
                                "final": True}]
    transcriber = StreamingTranscriber(_fake_open(events), uses_local_agreement=False)
    finals = asyncio.run(LiveSession(transcriber).run(_FakeProvider(), OCC))
    assert [s.text for s in finals] == ["hi there"] and finals[0].provenance == "final_live"


def test_sse_line_events_parse_data():
    lines = ['data: {"text": "bonjour"}', '', 'data: [DONE]', 'data: {"text": "monde", "final": true}']

    async def read_lines():
        async def _gen():
            for line in lines:
                yield line
        return _gen()

    open_stream = sse_line_events(read_lines)

    async def _collect():
        return [e async for e in open_stream(_empty())]

    async def _empty():
        return
        yield  # pragma: no cover

    events = asyncio.run(_collect())
    assert events == [{"text": "bonjour"}, {"text": "monde", "final": True}]

"""Cœur commun des transports LIVE « WebRTC-like » (Visio/LiveKit, Meet Media, Teams RTM).

Ces plateformes livrent toutes un flux audio DÉMULTIPLEXÉ par participant, mais leurs frames
natives ne portent NI numéro de séquence NI horodatage mural (ni parfois d'identité au niveau
frame). Le mapping vers `RawFrame` est donc identique : compteur de séquence synthétisé PAR
participant, horloge média cumulée PAR participant (Σ durées), wall-clock = heure d'arrivée
(injectable). Chaque transport ne diffère que par sa glue d'établissement (room/space/appel)
et son débit natif — factorisés ici pour rester maintenables.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import NamedTuple

from connector_service.contract import ExternalMeetingOccurrence
from connector_service.live.media import RawFrame


class DemuxedFrame(NamedTuple):
    """Frame audio déjà démultiplexée par participant (ce que fournit la glue transport).
    `payload` = PCM `pcm_s16le` entrelacé ; `sample_rate_hz` reflète le débit RÉELLEMENT
    livré (16 kHz LiveKit/Teams négociés, ~48 kHz Meet Media)."""

    participant_id: str
    payload: bytes
    sample_rate_hz: int = 16000
    channels: int = 1
    participant_name: str = ""
    track_id: str = ""


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


# open(occurrence) -> AsyncIterator[DemuxedFrame] : frames démuxées du transport établi.
DemuxSource = Callable[[ExternalMeetingOccurrence], AsyncIterator[DemuxedFrame]]


class DemuxFrameSource:
    """`FrameSource` générique : `DemuxedFrame` → `RawFrame` avec séquence et horloge média
    synthétisées par participant. Le transport réel (établissement + démux) est injecté."""

    def __init__(self, source: DemuxSource, *, now: Callable[[], str] | None = None) -> None:
        self._open = source
        self._now = now or _utc_now_iso

    async def frames(self, occurrence: ExternalMeetingOccurrence) -> AsyncIterator[RawFrame]:
        seq: dict[str, int] = {}
        elapsed_ms: dict[str, int] = {}
        async for df in self._open(occurrence):
            pid = df.participant_id
            n = seq[pid] = seq.get(pid, -1) + 1
            media_ts = elapsed_ms.get(pid, 0)
            samples = len(df.payload) // (2 * df.channels) if df.channels else 0
            if df.sample_rate_hz:
                elapsed_ms[pid] = media_ts + int(samples * 1000 / df.sample_rate_hz)
            yield RawFrame(
                participant_id=pid,
                payload=df.payload,
                sequence_number=n,
                media_timestamp_ms=media_ts,
                wall_clock_timestamp=self._now(),
                participant_name=df.participant_name,
                track_id=df.track_id or f"track-{pid}",
                sample_rate_hz=df.sample_rate_hz,
                channels=df.channels,
                encoding="pcm_s16le",
            )

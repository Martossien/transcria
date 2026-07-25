"""Vrai transport Visio/LiveKit (L1) — dep OPT-IN `livekit-rtc`, gate manuel.

Deux morceaux, séparés exprès :
- `AudioFanIn` : fusionne les flux audio PAR PARTICIPANT (ajoutés dynamiquement quand un
  participant s'abonne) en UN flux ordonné par arrivée. Indépendant de LiveKit → **testable
  en CI** avec des producteurs factices ; c'est la seule logique non triviale du transport.
- `livekit_demux_source` : le wiring `livekit.rtc` (connexion room, abonnement micro,
  `AudioStream` forcé à 16 kHz/mono) — **thin**, non testable sans serveur LiveKit, confirmé
  au gate manuel. Il ne fait qu'appeler `AudioFanIn.add_stream()` / `.stop()`.

À brancher : `DemuxFrameSource(livekit_demux_source(url, token))` → `visio_live_provider(...)`.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

from connector_service.contract import ExternalMeetingOccurrence
from connector_service.live._demux import DemuxedFrame

_STOP = object()


class AudioFanIn:
    """Fan-in de N flux audio (un par participant) → un seul flux. Un flux qui meurt ne tue
    pas la session ; `stop()` clôt proprement (annule les producteurs + sentinelle de fin)."""

    def __init__(self) -> None:
        self._q: asyncio.Queue[Any] = asyncio.Queue()
        self._tasks: set[asyncio.Task[None]] = set()
        self._stopped = False

    def add_stream(self, source: AsyncIterator[Any],
                   to_frame: Callable[[Any], DemuxedFrame]) -> None:
        """Enregistre un flux (l'`AudioStream` d'une piste) ; ses items sont convertis en
        `DemuxedFrame` via `to_frame` et poussés dans la file commune."""
        if self._stopped:
            return
        task = asyncio.ensure_future(self._drain(source, to_frame))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _drain(self, source: AsyncIterator[Any],
                     to_frame: Callable[[Any], DemuxedFrame]) -> None:
        try:
            async for item in source:
                await self._q.put(to_frame(item))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — un flux défaillant ne doit pas tuer les autres
            pass

    async def wait_producers(self) -> None:
        """Attend la fin de tous les flux courants (utile en arrêt gracieux / en test)."""
        await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def stop(self) -> None:
        self._stopped = True
        for task in list(self._tasks):
            task.cancel()
        self._q.put_nowait(_STOP)

    async def frames(self) -> AsyncIterator[DemuxedFrame]:
        while True:
            item = await self._q.get()
            if item is _STOP:
                return
            yield item


def livekit_demux_source(url: str, token: str, *, target_sample_rate_hz: int = 16000,
                         target_channels: int = 1) -> Callable[
                             [ExternalMeetingOccurrence], AsyncIterator[DemuxedFrame]]:
    """Source démuxée LiveKit réelle (dep opt-in `livekit`). Rejoint la room, s'abonne aux
    pistes MICRO, force 16 kHz/mono à la création de l'`AudioStream`, et yield un
    `DemuxedFrame` par frame. NON testé en CI → confirmé au gate manuel."""
    def _factory(occurrence: ExternalMeetingOccurrence) -> AsyncIterator[DemuxedFrame]:
        async def _open() -> AsyncIterator[DemuxedFrame]:
            from livekit import rtc  # dép opt-in, gate manuel

            room = rtc.Room()
            fan = AudioFanIn()

            def _to_frame(event: Any, participant: Any, publication: Any) -> DemuxedFrame:
                frame = event.frame
                return DemuxedFrame(
                    participant_id=str(participant.identity),
                    payload=bytes(frame.data),
                    sample_rate_hz=int(frame.sample_rate),
                    channels=int(frame.num_channels),
                    participant_name=str(getattr(participant, "name", "") or ""),
                    track_id=str(getattr(publication, "sid", "") or ""),
                )

            @room.on("track_subscribed")
            def _on_track(track: Any, publication: Any, participant: Any) -> None:
                if track.kind != rtc.TrackKind.KIND_AUDIO:
                    return
                # micro (ou source inconnue) — on écarte le partage d'écran/agents.
                if publication.source not in (rtc.TrackSource.SOURCE_MICROPHONE,
                                              rtc.TrackSource.SOURCE_UNKNOWN):
                    return
                stream = rtc.AudioStream(track, sample_rate=target_sample_rate_hz,
                                         num_channels=target_channels)

                def _convert(event: Any) -> DemuxedFrame:
                    return _to_frame(event, participant, publication)

                fan.add_stream(stream, _convert)

            @room.on("disconnected")
            def _on_disconnected(*_args: Any) -> None:
                fan.stop()

            await room.connect(url, token)
            try:
                async for frame in fan.frames():
                    yield frame
            finally:
                fan.stop()
                await room.disconnect()

        return _open()
    return _factory

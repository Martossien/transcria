#!/usr/bin/env python
"""GATE — transport LIVE Visio/LiveKit validé contre un VRAI serveur, sans navigateur.

Visio (La Suite numérique) repose sur LiveKit. C'est la voie OFFICIELLE du connecteur : le
bot navigateur n'est qu'un secours, et il ne devrait pas servir ici. Or ce transport n'avait
jamais été exécuté contre un serveur réel — seule sa logique de fusion des flux était couverte
en CI.

Ce gate ferme ce trou, et sans navigateur : le SDK LiveKit permet de PUBLIER une piste audio
depuis un fichier. Un « participant » synthétique diffuse de la vraie parole, notre transport
la capte par participant, et (optionnellement) la façade TranscrIA la transcrit.

Prérequis — un serveur LiveKit joignable. En local, mode développement :
    docker run -d --name livekit --network host livekit/livekit-server --dev
    (clés du mode dev : devkey / secret)

Usage :
    python scripts/gates/gate_visio_livekit.py --url ws://127.0.0.1:7880 \\
        --api-key devkey --api-secret secret --audio voix.wav \\
        [--transcribe http://127.0.0.1:7870 --token-file jeton.txt --language fr]
"""
from __future__ import annotations

import argparse
import asyncio
import struct
import sys
import wave
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from connector_service.contract import ExternalMeetingOccurrence  # noqa: E402
from connector_service.live._demux import DemuxFrameSource  # noqa: E402
from connector_service.live.facade_client import facade_transcriber  # noqa: E402
from connector_service.live.facade_stt import FacadeTranscriber  # noqa: E402
from connector_service.live.livekit_transport import livekit_demux_source  # noqa: E402
from connector_service.live.media import visio_live_provider  # noqa: E402
from connector_service.live.session import LiveSession  # noqa: E402
from connector_service.net_proxy import clear_proxy_env_if_bypassed  # noqa: E402

FRAME_MS = 10  # granularité de publication : ce que produit un micro réel


def _access_token(api_key: str, api_secret: str, room: str, identity: str) -> str:
    from livekit import api

    grants = api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True)
    return (api.AccessToken(api_key, api_secret)
            .with_identity(identity).with_name(identity).with_grants(grants).to_jwt())


async def publish_audio(url: str, api_key: str, api_secret: str, room_name: str,
                        wav_path: Path, identity: str, stop: asyncio.Event) -> None:
    """Publie un fichier WAV comme piste micro — remplace un participant humain.

    Le fichier est rejoué EN BOUCLE : un audio plus court que la session ferait mesurer du
    silence sans qu'on s'en aperçoive (erreur déjà commise sur un autre gate).
    """
    from livekit import rtc

    # Même contrainte que le transport : le client LiveKit ignore `no_proxy`. Le publieur
    # de test doit donc appliquer la même règle, sinon il échoue avant même de publier.
    clear_proxy_env_if_bypassed(url)

    with wave.open(str(wav_path)) as wav:
        rate, channels = wav.getframerate(), wav.getnchannels()
        pcm = wav.readframes(wav.getnframes())

    room = rtc.Room()
    await room.connect(url, _access_token(api_key, api_secret, room_name, identity))
    source = rtc.AudioSource(rate, channels)
    track = rtc.LocalAudioTrack.create_audio_track("voix", source)
    await room.local_participant.publish_track(track)

    samples_per_frame = int(rate * FRAME_MS / 1000)
    frame_bytes = samples_per_frame * 2 * channels
    try:
        offset = 0
        while not stop.is_set():
            if offset + frame_bytes > len(pcm):
                offset = 0                                    # boucle
            chunk = pcm[offset:offset + frame_bytes]
            offset += frame_bytes
            frame = rtc.AudioFrame(data=chunk, sample_rate=rate,
                                   num_channels=channels,
                                   samples_per_channel=samples_per_frame)
            await source.capture_frame(frame)
            await asyncio.sleep(FRAME_MS / 1000)
    finally:
        await room.disconnect()


class _FrameCounter:
    """Transcripteur factice : mesure ce qui arrive vraiment (nombre ET énergie).

    Compter les frames ne suffit pas : un flux peut couler en ne transportant que des zéros.
    """

    uses_local_agreement = False

    def __init__(self) -> None:
        self.per_participant: Counter[str] = Counter()
        self.peak = 0
        self.loud = 0

    def observe(self, frame) -> None:
        self.per_participant[frame.participant_id] += 1
        count = len(frame.payload) // 2
        if count:
            peak = max(abs(v) for v in struct.unpack(f"<{count}h", frame.payload[:count * 2]))
            self.peak = max(self.peak, peak)
            if peak > 500:
                self.loud += 1

    async def stream(self, frames):
        async for frame in frames:
            self.observe(frame)
        return
        yield  # pragma: no cover


class _Tee:
    """Mesure l'énergie captée PUIS délègue au vrai moteur STT."""

    def __init__(self, counter: _FrameCounter, inner) -> None:
        self._counter, self._inner = counter, inner
        self.uses_local_agreement = inner.uses_local_agreement

    def stream(self, frames):
        async def _tee():
            async for frame in frames:
                self._counter.observe(frame)
                yield frame
        return self._inner.stream(_tee())


async def main() -> int:
    parser = argparse.ArgumentParser(description="Gate du transport Visio/LiveKit")
    parser.add_argument("--url", default="ws://127.0.0.1:7880")
    parser.add_argument("--api-key", default="devkey")
    parser.add_argument("--api-secret", default="secret")
    parser.add_argument("--room", default="transcria-gate")
    parser.add_argument("--audio", required=True, help="WAV publié par le participant")
    parser.add_argument("--seconds", type=float, default=25.0)
    parser.add_argument("--transcribe", help="URL TranscrIA pour transcrire en direct")
    parser.add_argument("--token-file", help="fichier du jeton d'API TranscrIA")
    parser.add_argument("--language", default=None)
    args = parser.parse_args()

    counter = _FrameCounter()
    transcriber = counter
    if args.transcribe:
        token = Path(args.token_file).read_text().strip() if args.token_file else ""
        transcriber = _Tee(counter, FacadeTranscriber(
            facade_transcriber(args.transcribe, token, language=args.language)))

    print(f"→ serveur : {args.url}  | salle : {args.room}")
    print(f"→ audio publié : {args.audio}")

    stop = asyncio.Event()
    publisher = asyncio.ensure_future(publish_audio(
        args.url, args.api_key, args.api_secret, args.room, Path(args.audio), "Orateur", stop))
    await asyncio.sleep(4)                                    # laisse la piste s'établir

    occurrence = ExternalMeetingOccurrence(provider="visio", provider_account_id="gate",
                                           external_occurrence_id=args.room)
    token = _access_token(args.api_key, args.api_secret, args.room, "transcria-bot")
    provider = visio_live_provider(DemuxFrameSource(
        livekit_demux_source(args.url, token)))

    segments: list = []
    session = LiveSession(transcriber, on_final=segments.append)
    try:
        await asyncio.wait_for(session.run(provider, occurrence), timeout=args.seconds)
    except asyncio.TimeoutError:
        pass                                                  # durée du gate atteinte
    finally:
        stop.set()
        publisher.cancel()

    total = sum(counter.per_participant.values())
    print("\n────────── RÉSULTAT ──────────")
    print(f"frames captées : {total} | crête {counter.peak}/32767 | sonores {counter.loud}")
    print(f"participants   : {dict(counter.per_participant) or '—'}")
    for segment in segments:
        print(f"  [{segment.speaker or '?'}] {segment.text}")

    if total == 0:
        print("\n❌ AUCUNE frame — le transport ne reçoit rien.")
        return 1
    if counter.loud == 0:
        print("\n⚠️  Flux capté mais SILENCIEUX (aucune frame au-dessus du bruit).")
        return 1
    print("\n✅ TRANSPORT VISIO/LIVEKIT VALIDÉ : audio réel capté par participant.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

"""Gate Visio AUTOMATISÉ — publie un fichier audio dans une room LiveKit comme un
participant nommé (banc de gates sans humains, stack locale ~/visio-stack).

Signatures VÉRIFIÉES contre la lib livrée (rtc.AudioSource(sample_rate, num_channels),
rtc.AudioFrame(data, sample_rate, num_channels, samples_per_channel)). S'exécute dans
l'image `transcria-visio:latest` (elle embarque livekit) — jamais dans le venv du cœur.

Usage (un participant ; lancer N fois en parallèle pour N participants) :
  sudo docker run --rm --network host -v $PWD:/w -v $PWD/tests:/tests \
    -e LIVEKIT_URL=ws://127.0.0.1:7880 -e LIVEKIT_API_KEY=devkey -e LIVEKIT_API_SECRET=secret \
    --entrypoint python transcria-visio:latest /w/gates/gate_visio_publisher.py \
    <room> <nom-participant> /tests/test1.mp3

Scénarios du banc (cf. docs/VISIO_ZOOM_RUNNER.md) :
  solo        : 1 publisher test1.mp3                → 1 locuteur nommé, fusion sur-découpage
  salle       : 1 publisher test2.mp3 (2 voix)       → scission S1/S2 (micro partagé)
  multi       : 2 publishers (test1 + test2)         → pistes séparées + chevauchement + repisse corrélée
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys

SAMPLE_RATE = 48000
FRAME_MS = 10
SAMPLES = SAMPLE_RATE * FRAME_MS // 1000


def decode_pcm(path: str) -> bytes:
    """mp3/wav → PCM s16le mono 48 kHz. L'image visio n'embarque PAS ffmpeg : décoder
    côté HÔTE (`ffmpeg -i test1.mp3 -f s16le -ac 1 -ar 48000 test1.pcm`) et passer le
    .pcm — le repli ffmpeg ne sert que si l'environnement l'a."""
    if path.endswith(".pcm"):
        with open(path, "rb") as fh:
            return fh.read()
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-f", "s16le", "-acodec", "pcm_s16le",
         "-ac", "1", "-ar", str(SAMPLE_RATE), "-"],
        check=True, capture_output=True).stdout


async def publish(room_name: str, identity: str, audio_path: str) -> None:
    from livekit import api, rtc

    token = (api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
             .with_identity(identity).with_name(identity)
             .with_grants(api.VideoGrants(room_join=True, room=room_name,
                                          can_publish=True, can_subscribe=False))
             .to_jwt())
    pcm = decode_pcm(audio_path)
    room = rtc.Room()
    await room.connect(os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880"), token)
    source = rtc.AudioSource(SAMPLE_RATE, 1)
    track = rtc.LocalAudioTrack.create_audio_track("mic", source)
    await room.local_participant.publish_track(
        track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
    print(f"[{identity}] connecté à {room_name} — {len(pcm)//2//SAMPLE_RATE}s d'audio",
          flush=True)
    step = SAMPLES * 2
    loops = int(os.environ.get("GATE_LOOPS", "1"))
    for n in range(loops):                          # rejouer N fois : laisse au runner le
        for i in range(0, len(pcm) - step, step):   # temps de claim (cycle 30 s) et au bot
            frame = rtc.AudioFrame(pcm[i:i + step], SAMPLE_RATE, 1, SAMPLES)
            await source.capture_frame(frame)       # cadence tenue par la file interne
        print(f"[{identity}] passage {n + 1}/{loops} joué", flush=True)
    await asyncio.sleep(1.0)
    print(f"[{identity}] audio terminé — départ de la salle", flush=True)
    await room.disconnect()


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(__doc__)
        raise SystemExit(2)
    asyncio.run(publish(sys.argv[1], sys.argv[2], sys.argv[3]))

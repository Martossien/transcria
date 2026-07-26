"""STT LIVE par FENÊTRES sur la façade TranscrIA (`POST /v1/audio/transcriptions`).

Stratégie assumée : plutôt qu'un serveur de streaming dédié, on découpe l'audio de CHAQUE
locuteur en fenêtres et on les transcrit via la façade OpenAI-audio déjà en place — la clé
de voûte du plan. Conséquences directes, précieuses en réunion :

- **attribution native** : une fenêtre = un locuteur (le bot capte déjà une piste par
  participant), donc aucune diarisation à faire en direct ;
- **parole simultanée gérée** : les locuteurs ont des tampons INDÉPENDANTS et leurs fenêtres
  partent en parallèle — deux personnes qui se coupent produisent deux transcriptions, là où
  un flux mixé donnerait une bouillie ;
- **le direct reste un SUIVI** : la référence (`canonical`) sera produite par le pipeline
  batch sur l'enregistrement complet (ADR-001 D5).

Le découpage (`SpeakerBuffer`) est PUR et testable ; l'appel HTTP est injecté.
"""
from __future__ import annotations

import asyncio
import io
import struct
import wave
from collections.abc import AsyncIterator, Callable

from connector_service.contract import AudioFrame
from connector_service.live.agreement import Word
from connector_service.live.session import Hypothesis

SILENCE_PEAK = 500          # en deçà : la frame est considérée SILENCIEUSE (échelle s16)
MIN_WINDOW_S = 1.5          # ne pas transcrire des bribes plus courtes
MAX_WINDOW_S = 12.0         # borne haute : garantit un rendu régulier même sans pause
SILENCE_TO_CLOSE_S = 0.6    # pause qui clôt un tour de parole


def frame_peak(payload: bytes) -> int:
    """Amplitude crête d'une frame PCM `s16le` (détecte parole vs silence)."""
    count = len(payload) // 2
    if count == 0:
        return 0
    return max(abs(v) for v in struct.unpack(f"<{count}h", payload[:count * 2]))


def pcm_to_wav(pcm: bytes, sample_rate: int, channels: int = 1) -> bytes:
    """Emballe du PCM `s16le` en WAV (ce que la façade reçoit en pièce jointe)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


class SpeakerBuffer:
    """Tampon d'UN locuteur : accumule le PCM et décide quand la fenêtre est prête.

    Deux déclencheurs : une PAUSE suffisante après de la parole (fin de tour naturelle), ou
    la durée maximale (locuteur ininterrompu). Les fenêtres purement silencieuses sont
    jetées — inutile d'occuper le moteur STT avec du vide.
    """

    def __init__(self, sample_rate_hz: int, *, min_window_s: float = MIN_WINDOW_S,
                 max_window_s: float = MAX_WINDOW_S,
                 silence_to_close_s: float = SILENCE_TO_CLOSE_S,
                 silence_peak: int = SILENCE_PEAK) -> None:
        self._rate = max(int(sample_rate_hz), 1)
        self._min = min_window_s
        self._max = max_window_s
        self._silence_close = silence_to_close_s
        self._silence_peak = silence_peak
        self._pcm = bytearray()
        self._silence_s = 0.0
        self._voiced = False

    @property
    def duration_s(self) -> float:
        return len(self._pcm) / 2 / self._rate

    def add(self, payload: bytes) -> bytes | None:
        """Ajoute une frame. Retourne la fenêtre à transcrire si elle est prête, sinon None."""
        self._pcm.extend(payload)
        frame_s = len(payload) / 2 / self._rate
        if frame_peak(payload) > self._silence_peak:
            self._voiced = True
            self._silence_s = 0.0
        else:
            self._silence_s += frame_s
        ready = (self.duration_s >= self._max
                 or (self._voiced and self.duration_s >= self._min
                     and self._silence_s >= self._silence_close))
        return self.flush() if ready else None

    def flush(self) -> bytes | None:
        """Vide le tampon et rend la fenêtre — None si elle ne contient aucune parole."""
        pcm, voiced = bytes(self._pcm), self._voiced
        self._pcm = bytearray()
        self._silence_s = 0.0
        self._voiced = False
        return pcm if (voiced and pcm) else None


# transcrire(wav_bytes) -> texte. Injecté : la façade réelle en prod, un faux en CI.
TranscribeWindow = Callable[[bytes], str]


class FacadeTranscriber:
    """`LiveTranscriber` : fenêtres par locuteur → façade → `Hypothesis` attribuée.

    `uses_local_agreement=False` : chaque fenêtre est déjà stable (pas d'hypothèse révisée),
    on émet donc directement des tours finaux portant le nom du locuteur.
    """

    uses_local_agreement = False

    def __init__(self, transcribe: TranscribeWindow, *, max_parallel: int = 3,
                 **buffer_options) -> None:
        self._transcribe = transcribe
        self._buffer_options = buffer_options
        # Une réunion animée peut déclencher plusieurs fenêtres simultanées : on borne la
        # charge du moteur STT sans jamais bloquer la capture.
        self._slots = asyncio.Semaphore(max_parallel)

    async def _run_window(self, pcm: bytes, rate: int, speaker: str, name: str,
                          out: asyncio.Queue) -> None:
        async with self._slots:
            wav = pcm_to_wav(pcm, rate)
            try:
                text = await asyncio.get_running_loop().run_in_executor(
                    None, self._transcribe, wav)
            except Exception:  # noqa: BLE001 — une fenêtre perdue n'arrête pas la réunion
                return
            text = (text or "").strip()
            if not text:
                return
            words = [Word(tok, 0.0, 0.0) for tok in text.split()]
            await out.put(Hypothesis(committed=words, is_final=True, speaker=name or speaker))

    async def stream(self, frames: AsyncIterator[AudioFrame]) -> AsyncIterator[Hypothesis]:
        buffers: dict[str, SpeakerBuffer] = {}
        names: dict[str, str] = {}
        out: asyncio.Queue = asyncio.Queue()
        tasks: set[asyncio.Task] = set()

        def launch(pcm: bytes, rate: int, speaker: str) -> None:
            task = asyncio.ensure_future(
                self._run_window(pcm, rate, speaker, names.get(speaker, ""), out))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        async def drain() -> None:
            async for frame in frames:
                speaker = frame.participant_id or "?"
                if frame.participant_display_name:
                    names[speaker] = frame.participant_display_name
                buf = buffers.get(speaker)
                if buf is None:
                    buf = buffers[speaker] = SpeakerBuffer(frame.sample_rate_hz,
                                                           **self._buffer_options)
                window = buf.add(frame.payload)
                if window:
                    launch(window, frame.sample_rate_hz, speaker)
            for speaker, buf in buffers.items():          # fin de réunion : derniers tours
                rest = buf.flush()
                if rest:
                    launch(rest, buf._rate, speaker)      # noqa: SLF001 (même module)

        drainer = asyncio.ensure_future(drain())
        try:
            while True:
                if drainer.done() and not tasks and out.empty():
                    break
                try:
                    yield await asyncio.wait_for(out.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
            if not drainer.cancelled():
                capture_error = drainer.exception()         # remonte l'échec de la capture
                if capture_error is not None:
                    raise capture_error
        finally:
            drainer.cancel()
            for task in list(tasks):
                task.cancel()

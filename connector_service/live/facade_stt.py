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
import logging
import struct
import wave
from collections.abc import AsyncIterator, Callable

from connector_service.contract import AudioFrame
from connector_service.live.agreement import Word
from connector_service.live.session import Hypothesis

logger = logging.getLogger(__name__)

SILENCE_PEAK = 500          # en deçà : la frame est considérée SILENCIEUSE (échelle s16)
MIN_WINDOW_S = 1.5          # ne pas transcrire des bribes plus courtes
MAX_WINDOW_S = 12.0         # borne haute : garantit un rendu régulier même sans pause
SILENCE_TO_CLOSE_S = 0.6    # pause qui clôt un tour de parole
# Parole cumulée EXIGÉE pour soumettre une fenêtre au moteur. En deçà, la fenêtre est jetée :
# un moteur de type Whisper n'échoue pas sur du silence, il INVENTE du texte (mesuré : 12 s de
# silence pur → une phrase française complète et fausse). 0,35 s laisse passer les
# interjections courtes (« oui », « d'accord ») tout en écartant clics et respirations.
MIN_VOICED_S = 0.35


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
    la durée maximale (locuteur ininterrompu).

    ⚠ POURQUOI UNE QUANTITÉ MINIMALE DE PAROLE, et pas un simple « il y a eu du son » :
    un moteur STT de type Whisper à qui l'on soumet du silence n'échoue pas — il **invente**.
    Mesuré sur cette installation : 12 secondes de silence NUMÉRIQUE PUR ont produit une
    phrase française complète et parfaitement fausse. Avec l'ancienne règle (« une seule
    frame au-dessus du seuil suffit »), un claquement de clavier ou une respiration ouvrait
    une fenêtre de 12 s presque vide, aussitôt transformée en texte imaginaire. C'est ce qui
    polluait les transcriptions de réunion réelle.

    On exige donc une DURÉE CUMULÉE de parole, pas un booléen. Vérifié par ailleurs : dès
    qu'une fenêtre contient de la vraie parole, l'entourer de silence ne la dégrade pas —
    le problème est bien l'absence de parole, pas la présence de silence.
    """

    def __init__(self, sample_rate_hz: int, *, min_window_s: float = MIN_WINDOW_S,
                 max_window_s: float = MAX_WINDOW_S,
                 silence_to_close_s: float = SILENCE_TO_CLOSE_S,
                 silence_peak: int = SILENCE_PEAK,
                 min_voiced_s: float = MIN_VOICED_S) -> None:
        self._rate = max(int(sample_rate_hz), 1)
        self._min = min_window_s
        self._max = max_window_s
        self._silence_close = silence_to_close_s
        self._silence_peak = silence_peak
        self._min_voiced = min_voiced_s
        self._pcm = bytearray()
        self._silence_s = 0.0
        self._voiced_s = 0.0

    @property
    def duration_s(self) -> float:
        return len(self._pcm) / 2 / self._rate

    @property
    def voiced_s(self) -> float:
        """Durée cumulée des frames au-dessus du seuil de silence."""
        return self._voiced_s

    def add(self, payload: bytes) -> bytes | None:
        """Ajoute une frame. Retourne la fenêtre à transcrire si elle est prête, sinon None."""
        self._pcm.extend(payload)
        frame_s = len(payload) / 2 / self._rate
        if frame_peak(payload) > self._silence_peak:
            self._voiced_s += frame_s
            self._silence_s = 0.0
        else:
            self._silence_s += frame_s
        # La fermeture sur pause n'a de sens qu'après de la parole ; le plafond de durée
        # s'applique inconditionnellement, sans quoi un flux continu de silence ferait
        # croître le tampon sans fin.
        ready = (self.duration_s >= self._max
                 or (self._voiced_s > 0 and self.duration_s >= self._min
                     and self._silence_s >= self._silence_close))
        return self.flush() if ready else None

    def flush(self) -> bytes | None:
        """Vide le tampon et rend la fenêtre — None si elle ne contient pas ASSEZ de parole.

        Rendre `None` jette l'audio : c'est voulu. Une fenêtre sans parole exploitable n'a
        rien à donner, et la soumettre au moteur produirait du texte inventé (cf. la
        docstring de la classe) — pire qu'un silence, car indétectable en aval.
        """
        pcm, voiced_s = bytes(self._pcm), self._voiced_s
        self._pcm = bytearray()
        self._silence_s = 0.0
        self._voiced_s = 0.0
        if not pcm or voiced_s < self._min_voiced:
            if pcm and voiced_s > 0:
                logger.debug("fenêtre écartée : %.2f s de parole (< %.2f s requis)",
                             voiced_s, self._min_voiced)
            return None
        return pcm


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
        # Compteur d'échecs : sert à journaliser sans inonder (les fenêtres se
        # succèdent toutes les quelques secondes, par locuteur).
        self._failures = 0

    async def _run_window(self, pcm: bytes, rate: int, speaker: str, name: str,
                          out: asyncio.Queue) -> None:
        async with self._slots:
            wav = pcm_to_wav(pcm, rate)
            try:
                text = await asyncio.get_running_loop().run_in_executor(
                    None, self._transcribe, wav)
            except Exception as exc:  # noqa: BLE001 — une fenêtre perdue n'arrête pas la réunion
                # ...mais elle ne doit pas disparaître EN SILENCE. Sans cette trace, une
                # façade injoignable produit une réunion sans le moindre texte et sans le
                # moindre indice : c'est exactement ce qui s'est produit au premier essai
                # avec transcription (aucun segment, aucune erreur, aucune requête reçue).
                self._failures += 1
                if self._failures <= 3 or self._failures % 25 == 0:
                    logger.warning(
                        "fenêtre de transcription PERDUE (%d au total) pour « %s » : %r",
                        self._failures, name or speaker, exc)
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

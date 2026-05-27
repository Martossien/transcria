"""Wrapper Silero VAD via faster_whisper pour la détection de zones de parole.

Utilisé en pré-transcription pour ne soumettre à Cohere ASR que les zones vocales,
évitant les hallucinations sur silence, bruits de fond et sons non-vocaux.

Le model card Cohere recommande explicitement un VAD ou noise gate en amont :
  "The model benefits from prepending a noise gate or VAD model in order to prevent
   low-volume, floor noise from turning into hallucinations."
"""

import logging

import numpy as np

logger = logging.getLogger(__name__)

_SR = 16000
# Seuil Silero recommandé pour l'ASR : 0.5 (conservateur, évite les faux positifs)
_DEFAULT_THRESHOLD = 0.5
_DEFAULT_MIN_SPEECH_MS = 250
_DEFAULT_MIN_SILENCE_MS = 400
_DEFAULT_SPEECH_PAD_MS = 200
_DEFAULT_MAX_GAP_S = 0.5   # silences < 500ms entre deux zones → fusionner


class SileroVAD:
    """Détection d'activité vocale Silero via faster_whisper.get_vad_model().

    Utilisation typique :
        vad = SileroVAD()
        audio, sr = librosa.load(path, sr=16000, mono=True)
        chunks = vad.build_speech_chunks(audio)
        for chunk in chunks:
            segs = transcriber.transcribe(audio_path=None, audio_array=chunk["audio"])
            # ajuster timestamps : seg["start"] += chunk["start"]
    """

    def __init__(
        self,
        threshold: float = _DEFAULT_THRESHOLD,
        min_speech_duration_ms: int = _DEFAULT_MIN_SPEECH_MS,
        min_silence_duration_ms: int = _DEFAULT_MIN_SILENCE_MS,
        speech_pad_ms: int = _DEFAULT_SPEECH_PAD_MS,
        max_gap_s: float = _DEFAULT_MAX_GAP_S,
    ):
        self.threshold = threshold
        self.min_speech_duration_ms = min_speech_duration_ms
        self.min_silence_duration_ms = min_silence_duration_ms
        self.speech_pad_ms = speech_pad_ms
        self.max_gap_s = max_gap_s
        self._model = None
        self._VadOptions = None
        self._available: bool | None = None

    @property
    def available(self) -> bool:
        if self._available is None:
            try:
                from faster_whisper.vad import VadOptions, get_speech_timestamps  # noqa: F401
                self._available = True
            except (ImportError, Exception) as exc:
                logger.debug("SileroVAD non disponible: %s", exc)
                self._available = False
        return self._available

    def _load(self) -> None:
        if self._model is not None:
            return
        from faster_whisper.vad import VadOptions, get_speech_timestamps
        # get_speech_timestamps est une fonction module-level, pas une méthode du modèle
        self._model = get_speech_timestamps
        self._VadOptions = VadOptions
        logger.debug("Silero VAD chargé")

    def get_speech_timestamps(self, audio: np.ndarray, sample_rate: int = _SR) -> list[dict]:
        """Retourne les zones de parole détectées.

        Returns:
            Liste de {"start": float_s, "end": float_s} triée par start.
            Liste vide si VAD non disponible (fallback transparent pour l'appelant).
        """
        if not self.available:
            return []
        try:
            self._load()
            opts = self._VadOptions(
                threshold=self.threshold,
                min_speech_duration_ms=self.min_speech_duration_ms,
                min_silence_duration_ms=self.min_silence_duration_ms,
                speech_pad_ms=self.speech_pad_ms,
            )
            audio_f32 = audio.astype(np.float32)
            raw = self._model(audio_f32, opts, sampling_rate=sample_rate)
            timestamps = [
                {"start": chunk["start"] / sample_rate, "end": chunk["end"] / sample_rate}
                for chunk in raw
            ]
            logger.debug("VAD: %d zones de parole détectées", len(timestamps))
            return timestamps
        except Exception as exc:
            logger.warning("VAD: erreur détection, fallback désactivé: %s", exc)
            return []

    def build_speech_chunks(
        self,
        audio: np.ndarray,
        sample_rate: int = _SR,
        max_chunk_s: int = 30,
    ) -> list[dict]:
        """Construit des chunks audio contenant uniquement de la parole.

        Les zones de parole proches (< max_gap_s de silence) sont fusionnées
        pour éviter les micro-chunks. Les zones longues sont découpées en
        sous-chunks de max_chunk_s.

        Args:
            audio: tableau numpy float32 mono 16 kHz.
            max_chunk_s: durée maximale d'un chunk en secondes.

        Returns:
            Liste de {"start": float_s, "end": float_s, "audio": np.ndarray}.
            Si VAD indisponible : retourne un seul chunk avec l'audio complet
            découpé en blocs de max_chunk_s (comportement identique à l'ancien
            chunking 30s fixe — fallback transparent).
        """
        total_duration = len(audio) / sample_rate
        timestamps = self.get_speech_timestamps(audio, sample_rate)

        if not timestamps:
            # Fallback : chunking 30s classique sans filtrage
            logger.info("VAD indisponible ou aucune parole détectée — chunking 30s fixe")
            return self._fallback_chunks(audio, sample_rate, max_chunk_s, total_duration)

        # Fusionner les zones proches pour éviter les micro-chunks
        merged = [dict(timestamps[0])]
        for ts in timestamps[1:]:
            if ts["start"] - merged[-1]["end"] <= self.max_gap_s:
                merged[-1]["end"] = ts["end"]
            else:
                merged.append(dict(ts))

        speech_duration = sum(z["end"] - z["start"] for z in merged)
        logger.info(
            "VAD: %d zones fusionnées en %d segments (%.1f%% du signal = parole)",
            len(timestamps),
            len(merged),
            100.0 * speech_duration / max(total_duration, 0.001),
        )

        # Construire les chunks finaux
        chunks = []
        for zone in merged:
            start, end = zone["start"], zone["end"]
            if end - start < 0.3:
                continue
            pos = start
            while pos < end:
                chunk_end = min(pos + max_chunk_s, end)
                chunks.append({
                    "start": pos,
                    "end": chunk_end,
                    "audio": audio[int(pos * sample_rate):int(chunk_end * sample_rate)],
                })
                pos = chunk_end

        return chunks

    @staticmethod
    def _fallback_chunks(
        audio: np.ndarray, sample_rate: int, max_chunk_s: int, total_duration: float
    ) -> list[dict]:
        chunks = []
        pos = 0.0
        while pos < total_duration:
            end = min(pos + max_chunk_s, total_duration)
            chunks.append({
                "start": pos,
                "end": end,
                "audio": audio[int(pos * sample_rate):int(end * sample_rate)],
            })
            pos = end
        return chunks

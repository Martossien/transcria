import logging
import time as _time
from pathlib import Path

from transcria.stt.base_transcriber import BaseTranscriber

logger = logging.getLogger(__name__)

_MODEL_VRAM: dict[str, int] = {
    "tiny": 1000,
    "tiny.en": 1000,
    "base": 1000,
    "small": 2000,
    "small.en": 2000,
    "medium": 5000,
    "medium.en": 5000,
    "large-v1": 10000,
    "large-v2": 10000,
    "large-v3": 10000,
    "distil-large-v2": 6000,
    "turbo": 6000,
}

_SUPPORTED_LANGUAGES = {
    "english": "en",
    "french": "fr",
    "german": "de",
    "italian": "it",
    "spanish": "es",
    "portuguese": "pt",
    "dutch": "nl",
    "polish": "pl",
    "greek": "el",
    "chinese": "zh",
    "japanese": "ja",
    "korean": "ko",
    "vietnamese": "vi",
    "arabic": "ar",
    "russian": "ru",
    "turkish": "tr",
    "swedish": "sv",
    "hungarian": "hu",
    "czech": "cs",
    "danish": "da",
    "finnish": "fi",
    "norwegian": "no",
    "romanian": "ro",
    "slovak": "sk",
    "catalan": "ca",
    "croatian": "hr",
    "bulgarian": "bg",
    "ukrainian": "uk",
}


class WhisperTranscriber(BaseTranscriber):

    supported_languages = _SUPPORTED_LANGUAGES
    model_name = "whisper"

    def __init__(
        self,
        model_size: str = "large-v3",
        device: str | None = None,
        compute_type: str = "int8",
        cpu_threads: int = 4,
        chunk_length_s: int = 30,
        beam_size: int = 5,
        best_of: int = 5,
        vad_filter: bool = True,
    ):
        self.model_size = model_size
        self.device = device or self._detect_device()
        self.compute_type = compute_type
        self.cpu_threads = cpu_threads
        self.chunk_length_s = chunk_length_s
        self.beam_size = beam_size
        self.best_of = best_of
        self.vad_filter = vad_filter
        self._model = None
        self._runtime_model_size: str | None = None

    @property
    def vram_mb(self) -> int:
        return _MODEL_VRAM.get(self.model_size, 2000)

    @staticmethod
    def _detect_device() -> str:
        try:
            import torch
            if torch.cuda.is_available():
                return "cuda"
        except ImportError:
            pass
        return "cpu"

    @property
    def available(self) -> bool:
        try:
            import faster_whisper
            return True
        except ImportError:
            return False

    def load(self) -> bool:
        if self._model is not None and self._runtime_model_size == self.model_size:
            return True
        if not self.available:
            logger.warning("Faster-Whisper: dépendances manquantes (faster_whisper)")
            return False
        try:
            from faster_whisper import WhisperModel

            self.offload()
            logger.info(
                "Faster-Whisper: chargement modèle %s sur %s (%s bits)",
                self.model_size,
                self.device,
                self.compute_type,
            )
            self._model = WhisperModel(
                self.model_size,
                device=self.device,
                compute_type=self.compute_type,
                cpu_threads=self.cpu_threads,
                num_workers=1,
            )
            self._runtime_model_size = self.model_size
            return True
        except Exception as exc:
            logger.warning("Échec chargement Faster-Whisper: %s", exc)
            return False

    def transcribe(
        self,
        audio_path: Path,
        language: str = "fr",
        chunk_length_s: int = 30,
        progress_callback=None,
    ) -> list[dict]:
        if not self.load():
            return [{"error": "Faster-Whisper non disponible"}]

        _t0 = _time.time()
        ch_len = chunk_length_s or self.chunk_length_s
        lang_code = self.supported_languages.get(language.lower(), language)
        valid_codes = set(self.supported_languages.values())
        if lang_code not in valid_codes:
            lang_code = "fr"

        logger.info(
            "Transcription Faster-Whisper: %s, langue=%s, chunks=%ds",
            audio_path.name,
            lang_code,
            ch_len,
        )

        segments = []

        gen_segments, info = self._model.transcribe(
            str(audio_path),
            language=lang_code,
            beam_size=self.beam_size,
            best_of=self.best_of,
            chunk_length=ch_len,
            vad_filter=self.vad_filter,
        )

        for seg in gen_segments:
            segments.append({
                "start": round(seg.start, 3),
                "end": round(seg.end, 3),
                "text": seg.text.strip(),
            })

        elapsed = _time.time() - _t0
        logger.info(
            "Transcription Faster-Whisper terminée: %d segments en %.1fs "
            "(langue détectée: %s, proba=%.2f)",
            len(segments),
            elapsed,
            info.language,
            info.language_probability,
        )
        return segments

    def offload(self) -> None:
        if self._model is not None:
            del self._model
            self._model = None
            self._runtime_model_size = None
            import gc
            gc.collect()

    @classmethod
    def available_sizes(cls) -> list[str]:
        return list(_MODEL_VRAM.keys())

    @classmethod
    def vram_for_size(cls, size: str) -> int:
        return _MODEL_VRAM.get(size, 2000)

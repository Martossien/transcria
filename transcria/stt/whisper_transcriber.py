import logging
import time as _time
from pathlib import Path

from transcria.config.loader import _deep_merge, get_default_config
from transcria.stt.base_transcriber import BaseTranscriber
from transcria.stt.registry import ModelCatalogEntry, SttBackendDescriptor

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
        word_timestamps: bool = True,
        condition_on_previous_text: bool = False,
        no_speech_threshold: float | None = 0.2,
        compression_ratio_threshold: float | None = 2.0,
        log_prob_threshold: float | None = -1.0,
        hallucination_silence_threshold: float | None = 3.0,
        repetition_penalty: float = 1.0,
        no_repeat_ngram_size: int = 0,
        suppress_numerals: bool = False,
        hotwords: str | None = None,
        initial_prompt: str | None = None,
        collapse_repetition_loops: bool = True,
        repetition_loop_min_repeats: int = 4,
        repetition_loop_max_phrase_words: int = 10,
        repetition_loop_keep_repeats: int = 2,
    ):
        self.model_size = model_size
        raw_device = device or self._detect_device()
        if raw_device and ":" in raw_device:
            base, idx = raw_device.rsplit(":", 1)
            self.device = base
            self.device_index = int(idx)
        else:
            self.device = raw_device
            self.device_index = 0
        self.compute_type = compute_type
        self.cpu_threads = cpu_threads
        self.chunk_length_s = chunk_length_s
        self.beam_size = beam_size
        self.best_of = best_of
        self.vad_filter = vad_filter
        self.word_timestamps = word_timestamps
        self.condition_on_previous_text = condition_on_previous_text
        self.no_speech_threshold = no_speech_threshold
        self.compression_ratio_threshold = compression_ratio_threshold
        self.log_prob_threshold = log_prob_threshold
        self.hallucination_silence_threshold = hallucination_silence_threshold
        self.repetition_penalty = repetition_penalty
        self.no_repeat_ngram_size = no_repeat_ngram_size
        self.suppress_numerals = suppress_numerals
        self.hotwords = hotwords
        self.initial_prompt = initial_prompt
        self.collapse_repetition_loops = collapse_repetition_loops
        self.repetition_loop_min_repeats = repetition_loop_min_repeats
        self.repetition_loop_max_phrase_words = repetition_loop_max_phrase_words
        self.repetition_loop_keep_repeats = repetition_loop_keep_repeats
        self._model = None
        self._runtime_model_size: str | None = None

    @property
    def vram_mb(self) -> int:  # type: ignore[override]  # surcharge dynamique (dépend de model_size) de l'attribut int de la base
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
            import faster_whisper  # noqa: F401
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
                device_index=self.device_index,
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
        audio_path: Path | None,
        language: str = "fr",
        chunk_length_s: int | None = None,
        progress_callback=None,
        audio_array=None,
        sample_rate: int = 16000,
    ) -> list[dict]:
        if not self.load():
            return [{"error": "Faster-Whisper non disponible"}]
        assert self._model is not None  # load() == True garantit le modèle chargé

        _t0 = _time.time()
        ch_len = self.chunk_length_s if chunk_length_s is None else chunk_length_s
        lang_code = self.supported_languages.get(language.lower(), language)
        valid_codes = set(self.supported_languages.values())
        if lang_code not in valid_codes:
            lang_code = "fr"

        logger.info(
            "Transcription Faster-Whisper: %s, langue=%s, chunks=%ds",
            audio_path.name if audio_path else "audio_array",
            lang_code,
            ch_len,
        )

        segments = []
        audio_input = audio_array if audio_array is not None else str(audio_path)
        if audio_input is None:
            return [{"error": "Faster-Whisper: audio_path ou audio_array requis"}]

        gen_segments, info = self._model.transcribe(
            audio_input,
            language=lang_code,
            beam_size=self.beam_size,
            best_of=self.best_of,
            repetition_penalty=self.repetition_penalty,
            no_repeat_ngram_size=self.no_repeat_ngram_size,
            compression_ratio_threshold=self.compression_ratio_threshold,
            log_prob_threshold=self.log_prob_threshold,
            no_speech_threshold=self.no_speech_threshold,
            condition_on_previous_text=self.condition_on_previous_text,
            initial_prompt=self.initial_prompt,
            suppress_tokens=self._suppress_tokens(),
            word_timestamps=self.word_timestamps,
            chunk_length=ch_len,
            vad_filter=self.vad_filter,
            hallucination_silence_threshold=self.hallucination_silence_threshold,
            hotwords=self.hotwords,
        )

        for seg in gen_segments:
            text = seg.text.strip()
            loops: list = []
            if self.collapse_repetition_loops and text:
                from transcria.stt.anti_hallucination import collapse_repetition_loops

                text, loops = collapse_repetition_loops(
                    text,
                    min_repeats=self.repetition_loop_min_repeats,
                    max_phrase_words=self.repetition_loop_max_phrase_words,
                    keep_repeats=self.repetition_loop_keep_repeats,
                )

            item = {
                "start": round(seg.start, 3),
                "end": round(seg.end, 3),
                "text": text,
                "avg_logprob": getattr(seg, "avg_logprob", None),
                "compression_ratio": getattr(seg, "compression_ratio", None),
                "no_speech_prob": getattr(seg, "no_speech_prob", None),
            }
            words = self._extract_words(seg)
            if words:
                item["words"] = words
            if loops:
                item["hallucination_loops"] = loops
                item["text_before_loop_collapse"] = seg.text.strip()
            segments.append({
                key: value for key, value in item.items() if value is not None
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

    def _suppress_tokens(self) -> list[int]:
        if not self.suppress_numerals or self._model is None:
            return [-1]
        tokenizer = getattr(self._model, "hf_tokenizer", None)
        if tokenizer is None or not hasattr(tokenizer, "get_vocab"):
            return [-1]
        tokens = [-1]
        for token, token_id in tokenizer.get_vocab().items():
            if any(ch in "0123456789%$£€" for ch in token):
                tokens.append(token_id)
        return tokens

    @staticmethod
    def _extract_words(seg) -> list[dict]:
        words = []
        for word in getattr(seg, "words", None) or []:
            if word.start is None or word.end is None:
                continue
            words.append({
                "word": word.word,
                "start": round(float(word.start), 3),
                "end": round(float(word.end), 3),
                "probability": getattr(word, "probability", None),
            })
        return words

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


# --- Enregistrement au registre STT (vague C1) --------------------------------

def _effective_whisper_config(config: dict) -> dict:
    legacy = config.get("models", {}).get("whisper", {})
    current = config.get("whisper", {})
    defaults = get_default_config()["whisper"]
    return _deep_merge(_deep_merge(defaults, legacy), current)


def build(config: dict, device: str | None = None) -> WhisperTranscriber:
    whisper_cfg = _effective_whisper_config(config)

    return WhisperTranscriber(
        model_size=whisper_cfg["model_size"],
        device=device,
        compute_type=whisper_cfg["compute_type"],
        cpu_threads=whisper_cfg["cpu_threads"],
        chunk_length_s=whisper_cfg["chunk_length_s"],
        beam_size=whisper_cfg["beam_size"],
        best_of=whisper_cfg["best_of"],
        vad_filter=whisper_cfg["vad_filter"],
        word_timestamps=whisper_cfg["word_timestamps"],
        condition_on_previous_text=whisper_cfg["condition_on_previous_text"],
        no_speech_threshold=whisper_cfg["no_speech_threshold"],
        compression_ratio_threshold=whisper_cfg["compression_ratio_threshold"],
        log_prob_threshold=whisper_cfg["log_prob_threshold"],
        hallucination_silence_threshold=whisper_cfg["hallucination_silence_threshold"],
        repetition_penalty=whisper_cfg["repetition_penalty"],
        no_repeat_ngram_size=whisper_cfg["no_repeat_ngram_size"],
        suppress_numerals=whisper_cfg["suppress_numerals"],
        hotwords=whisper_cfg.get("hotwords"),
        initial_prompt=whisper_cfg.get("initial_prompt"),
        collapse_repetition_loops=whisper_cfg["collapse_repetition_loops"],
        repetition_loop_min_repeats=whisper_cfg["repetition_loop_min_repeats"],
        repetition_loop_max_phrase_words=whisper_cfg["repetition_loop_max_phrase_words"],
        repetition_loop_keep_repeats=whisper_cfg["repetition_loop_keep_repeats"],
    )


def vram_mb(config: dict) -> int:
    return WhisperTranscriber.vram_for_size(_effective_whisper_config(config)["model_size"])


DESCRIPTOR = SttBackendDescriptor(
    name="whisper",
    build=build,
    vram_mb=vram_mb,
    catalog=ModelCatalogEntry(
        repo="openai/whisper-large-v3",
        gated=False,
        license="MIT",
        license_url="https://huggingface.co/openai/whisper-large-v3",
        est_gb=3.1,
    ),
)

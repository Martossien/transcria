import logging
import time as _time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from transcria.stt.base_transcriber import BaseTranscriber

logger = logging.getLogger(__name__)

_GRANITE_MODEL_REPO = "ibm-granite/granite-speech-4.1-2b"
_MIN_TRANSFORMERS_VERSION = (4, 52, 1)
_SUPPORTED_LANGUAGES = {
    "english": "en",
    "french": "fr",
    "german": "de",
    "spanish": "es",
    "portuguese": "pt",
    "japanese": "ja",
}
_PROMPTS = {
    "asr_raw": "<|audio|>can you transcribe the speech into a written format?",
    "asr_punctuated": "<|audio|>transcribe the speech with proper punctuation and capitalization.",
    "keywords": "<|audio|>transcribe the speech to text. Keywords: {keywords}",
}


class GraniteTranscriber(BaseTranscriber):
    """Backend expérimental IBM Granite Speech 4.1 2B.

    Ce backend reste volontairement ASR pur : la diarisation continue d'être
    portée par pyannote dans le pipeline TranscrIA.
    """

    vram_mb = 6000
    supported_languages = _SUPPORTED_LANGUAGES
    model_name = "granite-speech-4.1-2b"

    def __init__(
        self,
        model_path: str | None = None,
        device: str | None = None,
        chunk_length_s: int = 30,
        max_new_tokens: int = 2000,
        max_new_tokens_per_second: float | None = 8.0,
        min_new_tokens: int = 64,
        torch_dtype: str = "bfloat16",
        prompt_mode: str = "asr_punctuated",
        prompt_asr_raw: str | None = None,
        prompt_asr_punctuated: str | None = None,
        prompt_keywords: str | None = None,
        keywords: list[str] | str | None = None,
        fix_mistral_regex: bool = True,
        collapse_repetition_loops: bool = True,
        repetition_loop_min_repeats: int = 4,
        repetition_loop_max_phrase_words: int = 10,
        repetition_loop_keep_repeats: int = 2,
    ):
        self.model_path = model_path or _GRANITE_MODEL_REPO
        self.device = device or self._detect_device()
        # 30 s par défaut : à 300 s le modèle hallucine massivement sur réunions longues
        # (constat docs/archive/GRANITE_STT_EXPERIMENT.md, test7.mp3).
        self.chunk_length_s = int(chunk_length_s or 30)
        self.max_new_tokens = int(max_new_tokens or 2000)
        self.max_new_tokens_per_second = (
            float(max_new_tokens_per_second)
            if max_new_tokens_per_second is not None and float(max_new_tokens_per_second) > 0
            else None
        )
        self.min_new_tokens = int(min_new_tokens or 64)
        self.torch_dtype = str(torch_dtype or "bfloat16")
        self.prompt_mode = str(prompt_mode or "asr_punctuated")
        self.prompts = {
            "asr_raw": prompt_asr_raw or _PROMPTS["asr_raw"],
            "asr_punctuated": prompt_asr_punctuated or _PROMPTS["asr_punctuated"],
            "keywords": prompt_keywords or _PROMPTS["keywords"],
        }
        self.keywords = keywords
        self.fix_mistral_regex = bool(fix_mistral_regex)
        self.collapse_repetition_loops = collapse_repetition_loops
        self.repetition_loop_min_repeats = repetition_loop_min_repeats
        self.repetition_loop_max_phrase_words = repetition_loop_max_phrase_words
        self.repetition_loop_keep_repeats = repetition_loop_keep_repeats
        self._model: Any = None  # transformers model (non typé) chargé paresseusement
        self._processor: Any = None
        self._tokenizer: Any = None
        self._metadata: dict = {
            "backend": "granite",
            "model_path": self.model_path,
            "prompt_mode": self.prompt_mode,
            "fix_mistral_regex": self.fix_mistral_regex,
            "max_new_tokens": self.max_new_tokens,
            "max_new_tokens_per_second": self.max_new_tokens_per_second,
            "min_new_tokens": self.min_new_tokens,
            "calls": 0,
            "segments": 0,
            "elapsed_s": 0.0,
        }

    @staticmethod
    def _detect_device() -> str:
        try:
            import torch

            if torch.cuda.is_available():
                return "cuda:0"
        except ImportError:
            pass
        return "cpu"

    @property
    def available(self) -> bool:
        try:
            import torch  # noqa: F401
            import transformers  # noqa: F401
        except ImportError:
            return False
        return self._transformers_version_ok()

    def load(self) -> bool:
        if self._model is not None:
            return True
        if not self.available:
            installed = self._installed_transformers_version()
            logger.warning(
                "Granite STT indisponible: transformers=%s, version minimale requise=%s",
                installed or "absent",
                ".".join(str(v) for v in _MIN_TRANSFORMERS_VERSION),
            )
            return False

        try:
            import torch
            from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor

            dtype = self._resolve_torch_dtype(torch)
            load_t0 = _time.time()
            processor_kwargs = {
                "trust_remote_code": True,
                "local_files_only": self._is_local_model_path(self.model_path),
            }
            if self.fix_mistral_regex:
                processor_kwargs["fix_mistral_regex"] = True
            # Verrou d'instanciation : `device_map=` déclenche accelerate.init_empty_weights()
            # (monkeypatch meta GLOBAL non thread-safe) — sérialisé pour ne pas corrompre un
            # chargement de modèle concurrent (ex. pyannote). Cf. transcria.gpu.model_load_lock.
            from transcria.gpu.model_load_lock import model_load_lock

            with model_load_lock():
                try:
                    self._processor = AutoProcessor.from_pretrained(self.model_path, **processor_kwargs)
                except TypeError as exc:
                    if "fix_mistral_regex" not in str(exc):
                        raise
                    processor_kwargs.pop("fix_mistral_regex", None)
                    self._processor = AutoProcessor.from_pretrained(self.model_path, **processor_kwargs)
                    self._metadata["fix_mistral_regex"] = False
                    logger.warning("Granite STT: fix_mistral_regex non supporté par cette version de transformers")

                self._tokenizer = self._processor.tokenizer
                model_kwargs = {
                    "device_map": self.device,
                    "dtype": dtype,
                    "trust_remote_code": True,
                    "local_files_only": self._is_local_model_path(self.model_path),
                }
                try:
                    self._model = AutoModelForSpeechSeq2Seq.from_pretrained(self.model_path, **model_kwargs)
                    self._metadata["dtype_arg"] = "dtype"
                except TypeError as exc:
                    if "dtype" not in str(exc):
                        raise
                    model_kwargs["torch_dtype"] = model_kwargs.pop("dtype")
                    self._model = AutoModelForSpeechSeq2Seq.from_pretrained(self.model_path, **model_kwargs)
                    self._metadata["dtype_arg"] = "torch_dtype"
                    logger.warning("Granite STT: fallback torch_dtype utilisé pour cette version de transformers")
            elapsed = _time.time() - load_t0
            self._metadata.update({
                "model_path": self.model_path,
                "device": self.device,
                "torch_dtype": self.torch_dtype,
                "load_s": round(elapsed, 3),
            })
            logger.info(
                "Granite STT chargé: modèle=%s device=%s dtype=%s fix_mistral_regex=%s durée=%.2fs",
                self.model_path,
                self.device,
                self.torch_dtype,
                self._metadata.get("fix_mistral_regex"),
                elapsed,
            )
            return True
        except Exception as exc:
            logger.warning("Échec chargement Granite STT: %s", exc)
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
            return [{"error": "Granite STT non disponible"}]

        import librosa
        import numpy as np
        import torch

        t0 = _time.time()
        ch_len = int(chunk_length_s or self.chunk_length_s)
        if audio_array is not None:
            audio = audio_array.astype(np.float32)
            sr = int(sample_rate or 16000)
            source = "audio_array"
        else:
            if audio_path is None:
                return [{"error": "Granite STT: audio_path ou audio_array requis"}]
            source = str(audio_path)
            audio, _sr = librosa.load(str(audio_path), sr=16000, mono=True)
            sr = int(_sr)

        total_samples = len(audio)
        if total_samples == 0:
            return []
        chunk_samples = max(1, ch_len * sr)
        total_duration = total_samples / sr
        total_chunks = int((total_samples + chunk_samples - 1) / chunk_samples)
        prompt = self._build_prompt()
        prompt_text = self._tokenizer.apply_chat_template(
            [{"role": "user", "content": prompt}],
            tokenize=False,
            add_generation_prompt=True,
        )
        logger.info(
            "Transcription Granite: source=%s langue=%s durée=%.1fs chunks=%d prompt_mode=%s",
            source,
            self.supported_languages.get(language.lower(), language),
            total_duration,
            total_chunks,
            self.prompt_mode,
        )

        segments: list[dict] = []
        for chunk_index, start_sample in enumerate(range(0, total_samples, chunk_samples), start=1):
            end_sample = min(start_sample + chunk_samples, total_samples)
            if end_sample - start_sample < sr * 0.5:
                continue
            chunk = torch.tensor(audio[start_sample:end_sample], dtype=torch.float32).unsqueeze(0)
            start_seconds = start_sample / sr
            end_seconds = end_sample / sr
            chunk_duration_s = end_seconds - start_seconds
            chunk_max_new_tokens = self._max_new_tokens_for_chunk(chunk_duration_s)

            model_inputs = self._processor(
                prompt_text,
                chunk,
                device=self.device,
                return_tensors="pt",
            ).to(self.device)
            with torch.no_grad():
                model_outputs = self._model.generate(
                    **model_inputs,
                    max_new_tokens=chunk_max_new_tokens,
                    do_sample=False,
                    num_beams=1,
                )
            num_input_tokens = model_inputs["input_ids"].shape[-1]
            new_tokens = model_outputs[0, num_input_tokens:].unsqueeze(0)
            text = self._tokenizer.batch_decode(
                new_tokens,
                add_special_tokens=False,
                skip_special_tokens=True,
            )[0].strip()
            loops: list = []
            if text and self.collapse_repetition_loops:
                text, loops = self._apply_loop_collapse(text)
            item = {
                "start": round(start_seconds, 3),
                "end": round(end_seconds, 3),
                "text": text,
                "backend": "granite",
                "granite_prompt_mode": self.prompt_mode,
            }
            if loops:
                item["hallucination_loops"] = loops
            if text:
                segments.append(item)
            if progress_callback:
                progress_callback(min(end_seconds / total_duration, 1.0))
            if chunk_index % 10 == 0 or chunk_index == total_chunks:
                logger.info("Progression Granite: %d/%d chunks", chunk_index, total_chunks)

        elapsed = _time.time() - t0
        self._metadata["calls"] = int(self._metadata.get("calls", 0)) + 1
        self._metadata["segments"] = int(self._metadata.get("segments", 0)) + len(segments)
        self._metadata["elapsed_s"] = round(float(self._metadata.get("elapsed_s", 0.0)) + elapsed, 3)
        self._metadata["last_audio_duration_s"] = round(total_duration, 3)
        self._metadata["last_chunks"] = total_chunks
        self._metadata["last_chunk_max_new_tokens"] = self._max_new_tokens_for_chunk(min(ch_len, total_duration))
        logger.info("Transcription Granite terminée: %d segments en %.1fs", len(segments), elapsed)
        return segments

    def get_metadata(self) -> dict:
        return dict(self._metadata)

    def offload(self) -> None:
        self._model = None
        self._processor = None
        self._tokenizer = None
        try:
            import gc

            import torch

            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass

    def _build_prompt(self) -> str:
        mode = self.prompt_mode if self.prompt_mode in self.prompts else "asr_punctuated"
        if mode == "keywords":
            keywords = self._keywords_text()
            if keywords:
                return self.prompts["keywords"].format(keywords=keywords)
            mode = "asr_punctuated"
        return self.prompts[mode]

    def _keywords_text(self) -> str:
        if isinstance(self.keywords, str):
            return self.keywords.strip()
        if isinstance(self.keywords, (list, tuple)):
            return ", ".join(str(item).strip() for item in self.keywords if str(item).strip())
        return ""

    def _max_new_tokens_for_chunk(self, chunk_duration_s: float) -> int:
        """Borne le budget de génération pour limiter les boucles sur chunks courts."""
        if self.max_new_tokens_per_second is None:
            return self.max_new_tokens
        scaled = int(max(self.min_new_tokens, round(float(chunk_duration_s) * self.max_new_tokens_per_second)))
        return max(1, min(self.max_new_tokens, scaled))

    def _apply_loop_collapse(self, text: str) -> tuple[str, list[dict]]:
        from transcria.stt.anti_hallucination import collapse_repetition_loops

        return collapse_repetition_loops(
            text,
            min_repeats=self.repetition_loop_min_repeats,
            max_phrase_words=self.repetition_loop_max_phrase_words,
            keep_repeats=self.repetition_loop_keep_repeats,
        )

    def _resolve_torch_dtype(self, torch):
        dtype = self.torch_dtype.lower().strip()
        if dtype in {"bf16", "bfloat16"}:
            return torch.bfloat16
        if dtype in {"fp16", "float16"}:
            return torch.float16
        if dtype in {"fp32", "float32"}:
            return torch.float32
        logger.warning("Granite STT: torch_dtype inconnu '%s', fallback bfloat16", self.torch_dtype)
        return torch.bfloat16

    @staticmethod
    def _is_local_model_path(model_id: str) -> bool:
        return Path(str(model_id)).exists()

    @staticmethod
    def _installed_transformers_version() -> str | None:
        try:
            return version("transformers")
        except PackageNotFoundError:
            return None

    @classmethod
    def _transformers_version_ok(cls) -> bool:
        installed = cls._installed_transformers_version()
        if not installed:
            return False
        return cls._version_tuple(installed) >= _MIN_TRANSFORMERS_VERSION

    @staticmethod
    def _version_tuple(value: str) -> tuple[int, int, int]:
        parts = []
        for raw in value.split(".")[:3]:
            digits = "".join(ch for ch in raw if ch.isdigit())
            parts.append(int(digits or 0))
        while len(parts) < 3:
            parts.append(0)
        return (parts[0], parts[1], parts[2])

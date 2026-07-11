import logging
import time as _time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from transcria.stt.base_transcriber import BaseTranscriber

logger = logging.getLogger(__name__)

_VOXTRAL_MODEL_REPO = "mistralai/Voxtral-Mini-3B-2507"
# VoxtralForConditionalGeneration + apply_transcription_request (ndarray) : 4.57+.
_MIN_TRANSFORMERS_VERSION = (4, 57, 0)
_SUPPORTED_LANGUAGES = {
    "english": "en",
    "french": "fr",
    "german": "de",
    "spanish": "es",
    "italian": "it",
    "portuguese": "pt",
    "dutch": "nl",
    "hindi": "hi",
}


class VoxtralTranscriber(BaseTranscriber):
    """Backend expérimental Mistral Voxtral Mini 3B (Apache-2.0).

    Mode « pure transcription » du modèle (``apply_transcription_request``), langue
    forcée nativement — pas de prompt à bricoler. Comme Granite, ce backend reste
    ASR pur : la diarisation est portée par pyannote/Sortformer dans le pipeline.
    Modèle NON-gated (aucun token HF requis), ~9,5 Go en bfloat16.
    """

    vram_mb = 11000
    supported_languages = _SUPPORTED_LANGUAGES
    model_name = "voxtral-mini-3b"

    def __init__(
        self,
        model_path: str | None = None,
        device: str | None = None,
        chunk_length_s: int = 30,
        max_new_tokens: int = 2000,
        max_new_tokens_per_second: float | None = 10.0,
        min_new_tokens: int = 64,
        torch_dtype: str = "bfloat16",
        collapse_repetition_loops: bool = True,
        repetition_loop_min_repeats: int = 4,
        repetition_loop_max_phrase_words: int = 10,
        repetition_loop_keep_repeats: int = 2,
    ):
        self.model_path = model_path or _VOXTRAL_MODEL_REPO
        self.device = device or self._detect_device()
        # 30 s par chunk : Voxtral accepte jusqu'à 30 min par requête, mais le
        # chunking par tours pyannote domine en pratique ; cette valeur borne les
        # chemins sans diarisation (même règle que les autres backends).
        self.chunk_length_s = int(chunk_length_s or 30)
        self.max_new_tokens = int(max_new_tokens or 2000)
        self.max_new_tokens_per_second = (
            float(max_new_tokens_per_second)
            if max_new_tokens_per_second is not None and float(max_new_tokens_per_second) > 0
            else None
        )
        self.min_new_tokens = int(min_new_tokens or 64)
        self.torch_dtype = str(torch_dtype or "bfloat16")
        self.collapse_repetition_loops = collapse_repetition_loops
        self.repetition_loop_min_repeats = repetition_loop_min_repeats
        self.repetition_loop_max_phrase_words = repetition_loop_max_phrase_words
        self.repetition_loop_keep_repeats = repetition_loop_keep_repeats
        self._model: Any = None
        self._processor: Any = None
        self._metadata: dict = {
            "backend": "voxtral",
            "model_path": self.model_path,
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
            from transformers import VoxtralForConditionalGeneration  # noqa: F401
        except ImportError:
            return False
        try:
            import mistral_common  # noqa: F401
        except ImportError:
            logger.warning("Voxtral STT indisponible: mistral-common absent (pip install 'mistral-common[audio]')")
            return False
        return self._transformers_version_ok()

    def load(self) -> bool:
        if self._model is not None:
            return True
        if not self.available:
            installed = self._installed_transformers_version()
            logger.warning(
                "Voxtral STT indisponible: transformers=%s (minimum %s) ou mistral-common absent",
                installed or "absent",
                ".".join(str(v) for v in _MIN_TRANSFORMERS_VERSION),
            )
            return False

        try:
            import torch
            from transformers import AutoProcessor, VoxtralForConditionalGeneration

            dtype = self._resolve_torch_dtype(torch)
            load_t0 = _time.time()
            # Chemin local configuré mais absent (ex. modèle téléchargé via la page
            # « Modèles » → cache HF) : repli sur l'identifiant HF officiel.
            resolved_path = self.model_path
            if resolved_path.startswith((".", "/")) and not Path(resolved_path).exists():
                logger.info(
                    "Voxtral STT: chemin local %s absent — repli sur %s (cache HF)",
                    resolved_path, _VOXTRAL_MODEL_REPO,
                )
                resolved_path = _VOXTRAL_MODEL_REPO
            self.model_path = resolved_path
            self._metadata["model_path"] = resolved_path
            local_only = self._is_local_model_path(self.model_path)
            # Même verrou que Granite/pyannote : device_map= passe par
            # accelerate.init_empty_weights (monkeypatch meta global non thread-safe).
            from transcria.gpu.model_load_lock import model_load_lock

            with model_load_lock():
                self._processor = AutoProcessor.from_pretrained(
                    self.model_path, local_files_only=local_only
                )
                self._model = VoxtralForConditionalGeneration.from_pretrained(
                    self.model_path,
                    device_map=self.device,
                    dtype=dtype,
                    local_files_only=local_only,
                )
            elapsed = _time.time() - load_t0
            self._metadata.update({
                "device": self.device,
                "torch_dtype": self.torch_dtype,
                "load_s": round(elapsed, 3),
            })
            logger.info(
                "Voxtral STT chargé: modèle=%s device=%s dtype=%s durée=%.2fs",
                self.model_path, self.device, self.torch_dtype, elapsed,
            )
            return True
        except Exception as exc:
            logger.warning("Échec chargement Voxtral STT: %s", exc)
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
            return [{"error": "Voxtral STT non disponible"}]

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
                return [{"error": "Voxtral STT: audio_path ou audio_array requis"}]
            source = str(audio_path)
            audio, _sr = librosa.load(str(audio_path), sr=16000, mono=True)
            sr = int(_sr)

        total_samples = len(audio)
        if total_samples == 0:
            return []
        lang_code = self._language_code(language)
        chunk_samples = max(1, ch_len * sr)
        total_duration = total_samples / sr
        total_chunks = int((total_samples + chunk_samples - 1) / chunk_samples)
        logger.info(
            "Transcription Voxtral: source=%s langue=%s durée=%.1fs chunks=%d",
            source, lang_code, total_duration, total_chunks,
        )

        segments: list[dict] = []
        for chunk_index, start_sample in enumerate(range(0, total_samples, chunk_samples), start=1):
            end_sample = min(start_sample + chunk_samples, total_samples)
            if end_sample - start_sample < sr * 0.5:
                continue
            chunk = audio[start_sample:end_sample]
            start_seconds = start_sample / sr
            end_seconds = end_sample / sr
            chunk_max_new_tokens = self._max_new_tokens_for_chunk(end_seconds - start_seconds)

            # Contrat processor (transformers 4.57) : un ndarray exige `format` (liste
            # alignée sur les audios) et seul return_tensors="pt" est supporté.
            inputs = self._processor.apply_transcription_request(
                language=lang_code,
                audio=chunk,
                model_id=self.model_path,
                sampling_rate=sr,
                format=["wav"],
                return_tensors="pt",
            ).to(self.device, dtype=self._resolve_torch_dtype(torch))
            with torch.no_grad():
                outputs = self._model.generate(
                    **inputs,
                    max_new_tokens=chunk_max_new_tokens,
                    do_sample=False,
                )
            num_input_tokens = inputs["input_ids"].shape[-1]
            text = self._processor.batch_decode(
                outputs[:, num_input_tokens:], skip_special_tokens=True
            )[0].strip()
            loops: list = []
            if text and self.collapse_repetition_loops:
                text, loops = self._apply_loop_collapse(text)
            item = {
                "start": round(start_seconds, 3),
                "end": round(end_seconds, 3),
                "text": text,
                "backend": "voxtral",
            }
            if loops:
                item["hallucination_loops"] = loops
            if text:
                segments.append(item)
            if progress_callback:
                progress_callback(min(end_seconds / total_duration, 1.0))
            if chunk_index % 10 == 0 or chunk_index == total_chunks:
                logger.info("Progression Voxtral: %d/%d chunks", chunk_index, total_chunks)

        elapsed = _time.time() - t0
        self._metadata["calls"] = int(self._metadata.get("calls", 0)) + 1
        self._metadata["segments"] = int(self._metadata.get("segments", 0)) + len(segments)
        self._metadata["elapsed_s"] = round(float(self._metadata.get("elapsed_s", 0.0)) + elapsed, 3)
        self._metadata["last_audio_duration_s"] = round(total_duration, 3)
        self._metadata["last_chunks"] = total_chunks
        logger.info("Transcription Voxtral terminée: %d segments en %.1fs", len(segments), elapsed)
        return segments

    def get_metadata(self) -> dict:
        return dict(self._metadata)

    def offload(self) -> None:
        self._model = None
        self._processor = None
        try:
            import gc

            import torch

            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass

    def _language_code(self, language: str) -> str:
        lang = str(language or "fr").strip().lower()
        if lang in self.supported_languages:
            return self.supported_languages[lang]
        if lang in self.supported_languages.values():
            return lang
        logger.warning("Voxtral STT: langue '%s' non supportée, repli fr", language)
        return "fr"

    def _max_new_tokens_for_chunk(self, chunk_duration_s: float) -> int:
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
        logger.warning("Voxtral STT: torch_dtype inconnu '%s', fallback bfloat16", self.torch_dtype)
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

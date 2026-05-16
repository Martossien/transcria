import logging
import os
from pathlib import Path

from transcria.stt.base_transcriber import BaseTranscriber

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

logger = logging.getLogger(__name__)

_COHERE_MODEL_REPO = "CohereLabs/cohere-transcribe-03-2026"
_SUPPORTED_LANGUAGES = {
    "english": "en", "french": "fr", "german": "de", "italian": "it",
    "spanish": "es", "portuguese": "pt", "greek": "el", "dutch": "nl",
    "polish": "pl", "chinese": "zh", "japanese": "ja", "korean": "ko",
    "vietnamese": "vi", "arabic": "ar",
}


class CohereTranscriber(BaseTranscriber):

    vram_mb = 6000
    supported_languages = _SUPPORTED_LANGUAGES
    model_name = "cohere-transcribe-03-2026"

    def __init__(
        self,
        model_path: str | None = None,
        device: str | None = None,
        chunk_length_s: int = 30,
        max_new_tokens: int = 448,
        repetition_penalty: float = 1.2,
        no_repeat_ngram_size: int = 3,
    ):
        self.model_path = model_path
        self.device = device or self._detect_device()
        self.chunk_length_s = chunk_length_s
        self.max_new_tokens = max_new_tokens
        self.repetition_penalty = repetition_penalty
        self.no_repeat_ngram_size = no_repeat_ngram_size
        self._model = None
        self._processor = None

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
            import torch
            import transformers
            return True
        except ImportError:
            return False

    def load(self) -> bool:
        if self._model is not None:
            return True
        if not self.available:
            logger.warning("Cohere ASR: dépendances manquantes (torch, transformers)")
            return False
        try:
            import torch
            from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor

            model_id = self.model_path or _COHERE_MODEL_REPO

            if model_id and not model_id.startswith(("CohereLabs/", "cohere/")):
                abs_path = os.path.abspath(model_id)
                if os.path.isdir(abs_path) and os.path.isfile(
                    os.path.join(abs_path, "config.json")
                ):
                    model_id = abs_path

            self._processor = AutoProcessor.from_pretrained(
                model_id, trust_remote_code=True
            )
            self._model = AutoModelForSpeechSeq2Seq.from_pretrained(
                model_id,
                torch_dtype=torch.bfloat16,
                device_map=self.device,
                trust_remote_code=True,
            )
            logger.info("Cohere ASR chargé sur %s depuis %s", self.device, model_id)
            return True
        except Exception as exc:
            logger.warning("Échec chargement Cohere ASR: %s", exc)
            return False

    def transcribe(
        self,
        audio_path: Path,
        language: str = "fr",
        chunk_length_s: int = 30,
        progress_callback=None,
    ) -> list[dict]:
        import torch
        import time as _time

        if not self.load():
            return [{"error": "Cohere ASR non disponible"}]

        import librosa
        import numpy as np

        _t0 = _time.time()
        ch_len = chunk_length_s or self.chunk_length_s
        logger.info(
            "Transcription Cohere: chargement audio %s", audio_path
        )
        audio, sr = librosa.load(str(audio_path), sr=16000, mono=True)
        total_samples = len(audio)
        sample_rate = 16000
        chunk_samples = ch_len * sample_rate
        segments: list[dict] = []
        total_duration = total_samples / sample_rate
        logger.info(
            "Audio chargé: %.1f min, %d échantillons, %d chunks attendus",
            total_duration / 60,
            total_samples,
            int(total_duration / ch_len) + 1,
        )

        lang_code = self.supported_languages.get(language.lower(), language)
        valid_codes = set(self.supported_languages.values())
        if lang_code not in valid_codes:
            lang_code = "fr"

        chunk_count = 0
        total_chunks = int(total_samples / chunk_samples) + 1
        for start_sample in range(0, total_samples, chunk_samples):
            end_sample = min(start_sample + chunk_samples, total_samples)
            chunk = audio[start_sample:end_sample]
            if len(chunk) < sample_rate * 0.5:
                continue

            inputs = self._processor(
                chunk,
                sampling_rate=sample_rate,
                return_tensors="pt",
                language=lang_code,
            )
            inputs = {k: v.to(self.device) for k, v in inputs.items()}
            if inputs["input_features"].dtype == torch.float32:
                inputs["input_features"] = inputs["input_features"].to(
                    torch.bfloat16
                )

            with torch.no_grad():
                decoder_attention_mask = torch.ones(
                    (1, 1), dtype=torch.long, device=self.device
                )
                generated_ids = self._model.generate(
                    inputs["input_features"],
                    max_new_tokens=self.max_new_tokens,
                    repetition_penalty=self.repetition_penalty,
                    no_repeat_ngram_size=self.no_repeat_ngram_size,
                    do_sample=False,
                    decoder_attention_mask=decoder_attention_mask,
                )

            text = self._processor.decode(
                generated_ids[0], skip_special_tokens=True
            )
            start_seconds = start_sample / sample_rate
            end_seconds = end_sample / sample_rate

            segments.append({
                "start": round(start_seconds, 3),
                "end": round(end_seconds, 3),
                "text": text.strip(),
            })

            chunk_count += 1
            if chunk_count % 10 == 0:
                logger.debug(
                    "Chunk %d/%d (%.1f%%)",
                    chunk_count,
                    total_chunks,
                    100.0 * chunk_count / total_chunks,
                )

            if progress_callback:
                progress_callback(start_seconds / total_duration)

        elapsed = _time.time() - _t0
        logger.info(
            "Transcription Cohere terminée: %d segments en %.1f min",
            len(segments),
            elapsed / 60,
        )
        return segments

    def offload(self) -> None:
        import gc
        import torch

        self._model = None
        self._processor = None
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            logger.debug("Cache CUDA vidé (Cohere ASR offloadé)")

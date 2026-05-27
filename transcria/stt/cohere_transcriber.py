import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from transcria.stt.base_transcriber import BaseTranscriber

if TYPE_CHECKING:
    import numpy

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
        collapse_repetition_loops: bool = True,
        repetition_loop_min_repeats: int = 4,
        repetition_loop_max_phrase_words: int = 10,
        repetition_loop_keep_repeats: int = 2,
        lexicon_biasing_enabled: bool = False,
        lexicon_biasing_terms: list[str] | None = None,
        lexicon_biasing_boost: float = 0.2,
        lexicon_biasing_start_boost: float = 0.05,
        lexicon_biasing_max_prefix_tokens: int = 20,
    ):
        self.model_path = model_path
        self.device = device or self._detect_device()
        self.chunk_length_s = chunk_length_s
        self.max_new_tokens = max_new_tokens
        self.repetition_penalty = repetition_penalty
        self.no_repeat_ngram_size = no_repeat_ngram_size
        self.collapse_repetition_loops = collapse_repetition_loops
        self.repetition_loop_min_repeats = repetition_loop_min_repeats
        self.repetition_loop_max_phrase_words = repetition_loop_max_phrase_words
        self.repetition_loop_keep_repeats = repetition_loop_keep_repeats
        self.lexicon_biasing_enabled = bool(lexicon_biasing_enabled)
        self.lexicon_biasing_terms = list(lexicon_biasing_terms or [])
        self.lexicon_biasing_boost = lexicon_biasing_boost
        self.lexicon_biasing_start_boost = lexicon_biasing_start_boost
        self.lexicon_biasing_max_prefix_tokens = lexicon_biasing_max_prefix_tokens
        self._model = None
        self._processor = None
        self._lexicon_logits_processor = None
        self._lexicon_biasing_stats: dict = {}

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
        audio_path: "Path | None",
        language: str = "fr",
        chunk_length_s: int = 30,
        progress_callback=None,
        audio_array: "numpy.ndarray | None" = None,
        sample_rate: int = 16000,
    ) -> list[dict]:
        """Transcrit un fichier audio ou un numpy array en segments {start, end, text}.

        Args:
            audio_path: chemin du fichier audio. Ignoré si audio_array est fourni.
            audio_array: tableau numpy (float32, mono, 16 kHz). Evite les I/O disque
                lors du chunking par tours pyannote.
            sample_rate: fréquence d'échantillonnage de audio_array (défaut 16000).
        """
        import time as _time

        import torch

        if not self.load():
            return [{"error": "Cohere ASR non disponible"}]

        import librosa
        import numpy as np

        _t0 = _time.time()
        ch_len = chunk_length_s or self.chunk_length_s

        if audio_array is not None:
            audio = audio_array.astype(np.float32)
            sr = sample_rate
            logger.debug("Transcription Cohere: audio fourni en mémoire (%d échantillons)", len(audio))
        else:
            logger.info("Transcription Cohere: chargement audio %s", audio_path)
            audio, sr = librosa.load(str(audio_path), sr=16000, mono=True)

        total_samples = len(audio)
        chunk_samples = ch_len * sr
        segments: list[dict] = []
        total_duration = total_samples / sr
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
        logits_processor = self._build_lexicon_logits_processor()

        chunk_count = 0
        total_chunks = int(total_samples / chunk_samples) + 1
        for start_sample in range(0, total_samples, chunk_samples):
            end_sample = min(start_sample + chunk_samples, total_samples)
            chunk = audio[start_sample:end_sample]
            if len(chunk) < sr * 0.5:
                continue

            inputs = self._processor(
                chunk,
                sampling_rate=sr,
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
                    logits_processor=logits_processor,
                )

            text = self._processor.decode(
                generated_ids[0], skip_special_tokens=True
            )
            start_seconds = start_sample / sr
            end_seconds = end_sample / sr

            text_raw = text.strip()
            item: dict = {
                "start": round(start_seconds, 3),
                "end": round(end_seconds, 3),
                "text": text_raw,
            }
            if text_raw:
                text_clean, loops = self._apply_loop_collapse(text_raw)
                if loops:
                    item["text"] = text_clean
                    item["hallucination_loops"] = loops
                    item["text_before_loop_collapse"] = text_raw
            segments.append(item)

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

    def _build_lexicon_logits_processor(self):
        """Construit une seule fois le processeur de biasing lexique Cohere."""
        if not self.lexicon_biasing_enabled or not self.lexicon_biasing_terms:
            self._lexicon_biasing_stats = {
                "enabled": bool(self.lexicon_biasing_enabled),
                "processor_created": False,
                "terms": list(self.lexicon_biasing_terms),
                "reason": "disabled" if not self.lexicon_biasing_enabled else "no_terms",
            }
            return None
        if self._lexicon_logits_processor is not None:
            return self._lexicon_logits_processor

        from transcria.stt.contextual_biasing import build_cohere_lexicon_processor

        processor, stats = build_cohere_lexicon_processor(
            self.lexicon_biasing_terms,
            self._processor.tokenizer,
            enabled=True,
            boost=self.lexicon_biasing_boost,
            start_boost=self.lexicon_biasing_start_boost,
            max_prefix_tokens=self.lexicon_biasing_max_prefix_tokens,
        )
        self._lexicon_logits_processor = processor
        self._lexicon_biasing_stats = stats
        if processor is not None:
            logger.info(
                "Biasing lexique Cohere activé: termes=%d séquences_tokens=%d boost=%.3f start_boost=%.3f max_prefix=%d",
                len(stats.get("terms", [])),
                stats.get("token_sequences", 0),
                self.lexicon_biasing_boost,
                self.lexicon_biasing_start_boost,
                self.lexicon_biasing_max_prefix_tokens,
            )
        else:
            logger.warning("Biasing lexique Cohere non créé: %s", stats.get("reason", "raison inconnue"))
        return processor

    def _apply_loop_collapse(self, text: str) -> tuple[str, list[dict]]:
        """Collapse repetition loops in text. Returns (cleaned_text, loops_metadata)."""
        if not self.collapse_repetition_loops:
            return text, []
        from transcria.stt.anti_hallucination import collapse_repetition_loops
        return collapse_repetition_loops(
            text,
            min_repeats=self.repetition_loop_min_repeats,
            max_phrase_words=self.repetition_loop_max_phrase_words,
            keep_repeats=self.repetition_loop_keep_repeats,
        )

    def offload(self) -> None:
        import gc

        import torch

        self._model = None
        self._processor = None
        self._lexicon_logits_processor = None
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            logger.debug("Cache CUDA vidé (Cohere ASR offloadé)")

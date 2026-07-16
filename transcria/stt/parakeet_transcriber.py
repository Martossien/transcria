import logging
import os
import time as _time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from transcria.config.loader import _deep_merge, get_default_config
from transcria.gpu.model_load_lock import model_load_lock
from transcria.stt.anti_hallucination import collapse_repetition_loops
from transcria.stt.base_transcriber import BaseTranscriber
from transcria.stt.registry import ModelCatalogEntry, SttBackendDescriptor

if TYPE_CHECKING:
    import numpy

logger = logging.getLogger(__name__)

_PARAKEET_MODEL_REPO = "nvidia/parakeet-tdt-0.6b-v3"
_SUPPORTED_LANGUAGES = {
    "bulgarian": "bg", "croatian": "hr", "czech": "cs", "danish": "da",
    "dutch": "nl", "english": "en", "estonian": "et", "finnish": "fi",
    "french": "fr", "german": "de", "greek": "el", "hungarian": "hu",
    "italian": "it", "latvian": "lv", "lithuanian": "lt", "maltese": "mt",
    "polish": "pl", "portuguese": "pt", "romanian": "ro", "slovak": "sk",
    "slovenian": "sl", "spanish": "es", "swedish": "sv",
    "russian": "ru", "ukrainian": "uk",
}


class ParakeetTranscriber(BaseTranscriber):
    """Backend experimental NVIDIA Parakeet TDT 0.6B v3.

    Ce backend utilise NeMo (nemo.collections.asr) et son API native
    model.transcribe() plutot que le pipeline Transformers generate().
    La diarisation reste portee par pyannote dans le pipeline TranscrIA.

    Limites connues documentees (cf. docs/PARAKEET_STT_INTEGRATION.md) :
    - Pas de word boosting / hotwords (contrairement a Whisper/Cohere)
    - ITN FR peut transcrire des nombres en lettres (ex: 09:30 → neuf heures trente)
    - Consommation VRAM proportionnelle a la duree audio (pre-chunking integre)
    - strategy=beam casse les timestamps dans NeMo 2.7.3 (defaut greedy_batch)
    - Risque de phrases sautees sur tours de parole courts (HF #12, greedy TDT)
    """

    vram_mb = 8000
    supported_languages = _SUPPORTED_LANGUAGES
    model_name = "parakeet-tdt-0.6b-v3"

    def __init__(
        self,
        model_path: str | None = None,
        device: str | None = None,
        use_local_attention: bool = True,
        att_context_size: tuple[int, int] = (256, 256),
        decoding_strategy: str = "greedy_batch",
        decoding_beam_size: int = 2,
        max_chunk_duration_s: int = 1200,
        collapse_repetition_loops: bool = True,
        repetition_loop_min_repeats: int = 4,
        repetition_loop_max_phrase_words: int = 10,
        repetition_loop_keep_repeats: int = 2,
    ):
        self.model_path = model_path or _PARAKEET_MODEL_REPO
        self.device = device or self._detect_device()
        self.use_local_attention = bool(use_local_attention)
        self.att_context_size = (
            int(att_context_size[0]),
            int(att_context_size[1]),
        )
        self.decoding_strategy = str(decoding_strategy or "greedy_batch")
        self.decoding_beam_size = int(decoding_beam_size or 2)
        self.max_chunk_duration_s = int(max_chunk_duration_s or 1200)
        self.collapse_repetition_loops = collapse_repetition_loops
        self.repetition_loop_min_repeats = repetition_loop_min_repeats
        self.repetition_loop_max_phrase_words = repetition_loop_max_phrase_words
        self.repetition_loop_keep_repeats = repetition_loop_keep_repeats
        self._model: Any = None  # nemo ASRModel (non typé) chargé paresseusement
        self._metadata: dict = {
            "backend": "parakeet",
            "model_path": self.model_path,
            "decoding_strategy": self.decoding_strategy,
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
            import nemo.collections.asr  # noqa: F401
            import torch  # noqa: F401
        except ImportError:
            return False
        return True

    def load(self) -> bool:
        if self._model is not None:
            return True
        if not self.available:
            logger.warning("Parakeet STT indisponible: nemo_toolkit[asr] absent")
            return False

        os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

        try:
            import torch
            from nemo.collections.asr.models import ASRModel
            from omegaconf import open_dict

            gpu_id = 0
            if self.device.startswith("cuda:"):
                gpu_id = int(self.device.split(":")[1])
            elif self.device == "cuda":
                gpu_id = 0
            if gpu_id > 0 and torch.cuda.is_available():
                torch.cuda.set_device(gpu_id)
                logger.info("Parakeet STT: CUDA device force sur %s", self.device)

            load_t0 = _time.time()

            with model_load_lock():  # sérialise l'instanciation (cf. model_load_lock)
                self._model = ASRModel.from_pretrained(self.model_path)

            desired = self.decoding_strategy
            current = str(self._model.cfg.decoding.strategy)
            if desired != current:
                cfg = self._model.cfg.decoding
                with open_dict(cfg):
                    cfg.strategy = desired
                if desired == "beam":
                    with open_dict(cfg):
                        cfg.beam.beam_size = self.decoding_beam_size
                        cfg.beam.return_best_hypothesis = True
                self._model.change_decoding_strategy(cfg)
                logger.info(
                    "Parakeet STT: strategie decodage changee %s → %s (beam_size=%s)",
                    current,
                    desired,
                    self.decoding_beam_size if desired == "beam" else "N/A",
                )
            else:
                logger.info("Parakeet STT: strategie decodage conservee (%s)", current)

            if self.use_local_attention:
                self._model.change_attention_model(
                    self_attention_model="rel_pos_local_attn",
                    att_context_size=list(self.att_context_size),
                )

            elapsed = _time.time() - load_t0
            self._metadata.update({
                "model_path": self.model_path,
                "device": self.device,
                "use_local_attention": self.use_local_attention,
                "att_context_size": list(self.att_context_size),
                "decoding_strategy_applied": desired if desired != current else current,
                "load_s": round(elapsed, 3),
            })
            logger.info(
                "Parakeet STT charge: modele=%s device=%s local_attn=%s att_ctx=%s duree=%.2fs",
                self.model_path,
                self.device,
                self.use_local_attention,
                self.att_context_size,
                elapsed,
            )
            return True
        except Exception as exc:
            logger.warning("Echec chargement Parakeet STT: %s", exc)
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
            return [{"error": "Parakeet STT non disponible"}]

        import librosa
        import numpy as np

        t0 = _time.time()
        if audio_array is not None:
            audio = audio_array.astype(np.float32)
            sr = int(sample_rate or 16000)
            source = "audio_array"
        else:
            if audio_path is None:
                return [{"error": "Parakeet STT: audio_path ou audio_array requis"}]
            source = str(audio_path)
            audio, _sr = librosa.load(str(audio_path), sr=16000, mono=True)
            sr = int(_sr)

        total_samples = len(audio)
        if total_samples == 0:
            return []
        total_duration: float = total_samples / sr
        logger.info(
            "Transcription Parakeet: source=%s duree=%.1fs",
            source,
            total_duration,
        )

        segments: list[dict] = []

        if total_duration > self.max_chunk_duration_s:
            segments = self._transcribe_chunked(
                audio, sr, total_duration, total_samples
            )
        else:
            segments = self._transcribe_single(audio, total_duration, 0.0)

        elapsed = _time.time() - t0
        self._metadata["calls"] = int(self._metadata.get("calls", 0)) + 1
        self._metadata["segments"] = int(self._metadata.get("segments", 0)) + len(segments)
        self._metadata["elapsed_s"] = round(float(self._metadata.get("elapsed_s", 0.0)) + elapsed, 3)
        self._metadata["last_audio_duration_s"] = round(total_duration, 3)
        logger.info("Transcription Parakeet terminee: %d segments en %.1fs", len(segments), elapsed)
        return segments

    def _transcribe_chunked(
        self,
        audio: "numpy.ndarray",
        sr: int,
        total_duration: float,
        total_samples: int,
    ) -> list[dict]:
        chunk_samples = self.max_chunk_duration_s * sr
        total_chunks = int((total_samples + chunk_samples - 1) / chunk_samples)
        logger.info(
            "Parakeet: pre-chunking actif — %d chunks de %ds max (audio %.1f min)",
            total_chunks,
            self.max_chunk_duration_s,
            total_duration / 60,
        )

        segments: list[dict] = []
        for chunk_index, start_sample in enumerate(range(0, total_samples, chunk_samples), start=1):
            end_sample = min(start_sample + chunk_samples, total_samples)
            chunk = audio[start_sample:end_sample]
            if len(chunk) < sr * 0.5:
                continue
            offset = start_sample / sr
            segs = self._transcribe_single(chunk, (end_sample - start_sample) / sr, offset)
            segments.extend(segs)
            if chunk_index % 5 == 0 or chunk_index == total_chunks:
                logger.info(
                    "Parakeet: chunk %d/%d (%d segments cumules)",
                    chunk_index,
                    total_chunks,
                    len(segments),
                )

        return segments

    def _transcribe_single(
        self,
        audio: "numpy.ndarray",
        chunk_duration: float,
        offset: float,
    ) -> list[dict]:
        try:
            output = self._model.transcribe([audio], timestamps=True)
        except Exception as exc:
            logger.exception("Echec transcription Parakeet")
            return [{"error": f"Parakeet STT: {exc}"}]

        hypothesis = output[0]
        full_text = getattr(hypothesis, "text", "").strip()
        timestamps = getattr(hypothesis, "timestamp", None) or {}

        word_ts = timestamps.get("word", [])
        seg_ts = timestamps.get("segment", [])

        segments: list[dict] = []
        if seg_ts:
            for seg in seg_ts:
                seg_text = str(seg.get("segment", "")).strip()
                if not seg_text:
                    continue
                seg_start = float(seg.get("start", 0)) + offset
                seg_end = float(seg.get("end", 0)) + offset
                if seg_end - seg_start < 0.05:
                    continue
                item: dict = {
                    "start": round(seg_start, 3),
                    "end": round(seg_end, 3),
                    "text": seg_text,
                    "backend": "parakeet",
                }
                words_in_seg = [
                    {
                        "word": str(w.get("word", "")),
                        "start": round(float(w.get("start", 0)) + offset, 3),
                        "end": round(float(w.get("end", 0)) + offset, 3),
                    }
                    for w in word_ts
                    if float(w.get("start", 0)) >= seg_start - offset - 0.01
                    and float(w.get("end", 0)) <= seg_end - offset + 0.01
                ]
                if words_in_seg:
                    item["words"] = words_in_seg
                loops: list = []
                if seg_text and self.collapse_repetition_loops:
                    cleaned, loops = self._apply_loop_collapse(seg_text)
                    if loops:
                        item["text"] = cleaned
                        item["hallucination_loops"] = loops
                segments.append(item)
        elif full_text:
            item = {
                "start": round(offset, 3),
                "end": round(offset + chunk_duration, 3),
                "text": full_text,
                "backend": "parakeet",
            }
            if word_ts:
                item["words"] = [
                    {
                        "word": str(w.get("word", "")),
                        "start": round(float(w.get("start", 0)) + offset, 3),
                        "end": round(float(w.get("end", 0)) + offset, 3),
                    }
                    for w in word_ts
                    if str(w.get("word", "")).strip()
                ]
            loops = []
            if full_text and self.collapse_repetition_loops:
                cleaned, loops = self._apply_loop_collapse(full_text)
                if loops:
                    item["text"] = cleaned
                    item["hallucination_loops"] = loops
            segments.append(item)

        return segments

    def get_metadata(self) -> dict:
        return dict(self._metadata)

    def offload(self) -> None:
        self._model = None
        try:
            import gc

            import torch

            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass

    def _apply_loop_collapse(self, text: str) -> tuple[str, list[dict]]:
        if not self.collapse_repetition_loops:
            return text, []

        return collapse_repetition_loops(
            text,
            min_repeats=self.repetition_loop_min_repeats,
            max_phrase_words=self.repetition_loop_max_phrase_words,
            keep_repeats=self.repetition_loop_keep_repeats,
        )


# --- Enregistrement au registre STT (vague C1) --------------------------------

def _effective_parakeet_config(config: dict) -> dict:
    current = config.get("parakeet", {})
    defaults = get_default_config()["parakeet"]
    return _deep_merge(defaults, current)


def build(config: dict, device: str | None = None) -> ParakeetTranscriber:
    parakeet_cfg = _effective_parakeet_config(config)
    att_ctx = parakeet_cfg.get("att_context_size", [256, 256])
    return ParakeetTranscriber(
        model_path=parakeet_cfg.get("model_id"),
        device=device,
        use_local_attention=parakeet_cfg.get("use_local_attention", True),
        att_context_size=(int(att_ctx[0]), int(att_ctx[1])),
        decoding_strategy=parakeet_cfg.get("decoding_strategy", "greedy_batch"),
        decoding_beam_size=parakeet_cfg.get("decoding_beam_size", 2),
        max_chunk_duration_s=parakeet_cfg.get("max_chunk_duration_s", 1200),
        collapse_repetition_loops=parakeet_cfg.get("collapse_repetition_loops", True),
        repetition_loop_min_repeats=parakeet_cfg.get("repetition_loop_min_repeats", 4),
        repetition_loop_max_phrase_words=parakeet_cfg.get("repetition_loop_max_phrase_words", 10),
        repetition_loop_keep_repeats=parakeet_cfg.get("repetition_loop_keep_repeats", 2),
    )


def vram_mb(config: dict) -> int:
    return int(config.get("gpu", {}).get("parakeet_vram_mb", get_default_config()["gpu"]["parakeet_vram_mb"]))


DESCRIPTOR = SttBackendDescriptor(
    name="parakeet",
    build=build,
    vram_mb=vram_mb,
    catalog=ModelCatalogEntry(
        repo="nvidia/parakeet-tdt-0.6b-v3",
        gated=False,
        license="CC-BY-4.0",
        license_url="https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3",
        est_gb=2.5,
    ),
)

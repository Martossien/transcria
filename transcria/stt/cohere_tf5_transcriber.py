import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from transcria.stt.base_transcriber import BaseTranscriber

if TYPE_CHECKING:
    import numpy

logger = logging.getLogger(__name__)

_COHERE_MODEL_REPO = "CohereLabs/cohere-transcribe-03-2026"
_SUPPORTED_LANGUAGES = {
    "english": "en", "french": "fr", "german": "de", "italian": "it",
    "spanish": "es", "portuguese": "pt", "greek": "el", "dutch": "nl",
    "polish": "pl", "chinese": "zh", "japanese": "ja", "korean": "ko",
    "vietnamese": "vi", "arabic": "ar",
}


class CohereTf5Transcriber(BaseTranscriber):
    """Backend Cohere ASR natif Transformers 5, expérimental et opt-in.

    Il charge `CohereAsrForConditionalGeneration`, indisponible dans la pile
    Transformers 4.x historique. `tf5_site` permet de pointer vers une
    installation isolée créée avec `pip --target`, sans modifier le venv projet.
    """

    vram_mb = 6000
    supported_languages = _SUPPORTED_LANGUAGES
    model_name = "cohere_tf5"
    concurrent_safe = False

    def __init__(
        self,
        model_path: str | None = None,
        model_revision: str | None = None,
        device: str | None = None,
        tf5_site: str | None = None,
        timeout_s: int = 7200,
        chunk_length_s: int = 30,
        max_new_tokens: int = 448,
        punctuation: bool = True,
        batch_size: int = 96,
        repetition_penalty: float = 1.2,
        no_repeat_ngram_size: int = 4,
        collapse_repetition_loops: bool = True,
        repetition_loop_min_repeats: int = 4,
        repetition_loop_max_phrase_words: int = 10,
        repetition_loop_keep_repeats: int = 2,
    ):
        self.model_path = model_path or _COHERE_MODEL_REPO
        self.model_revision = model_revision.strip() if isinstance(model_revision, str) and model_revision.strip() else None
        self.device = device or self._detect_device()
        self.tf5_site = tf5_site
        self.timeout_s = int(timeout_s or 7200)
        self.chunk_length_s = int(chunk_length_s or 30)
        self.max_new_tokens = int(max_new_tokens)
        self.punctuation = bool(punctuation)
        self.batch_size = max(1, int(batch_size or 1))
        self.repetition_penalty = float(repetition_penalty)
        self.no_repeat_ngram_size = int(no_repeat_ngram_size)
        self.collapse_repetition_loops = bool(collapse_repetition_loops)
        self.repetition_loop_min_repeats = int(repetition_loop_min_repeats)
        self.repetition_loop_max_phrase_words = int(repetition_loop_max_phrase_words)
        self.repetition_loop_keep_repeats = int(repetition_loop_keep_repeats)
        self._last_transcribe_metadata: dict = {}

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
        return not self.tf5_site or os.path.isdir(os.path.abspath(self.tf5_site))

    def load(self) -> bool:
        if not self.available:
            logger.warning("Cohere TF5 indisponible: tf5_site absent (%s)", self.tf5_site)
            return False
        return True

    @staticmethod
    def _resolve_model_id(model_id: str) -> str:
        if model_id and not model_id.startswith(("CohereLabs/", "cohere/")):
            abs_path = os.path.abspath(model_id)
            if os.path.isdir(abs_path) and os.path.isfile(os.path.join(abs_path, "config.json")):
                return abs_path
        return model_id

    def transcribe(
        self,
        audio_path: "Path | None",
        language: str = "fr",
        chunk_length_s: int | None = None,
        progress_callback=None,
        audio_array: "numpy.ndarray | None" = None,
        sample_rate: int = 16000,
    ) -> list[dict]:
        if not self.load():
            return [{"error": "Cohere TF5 non disponible"}]

        import librosa
        import numpy as np

        started = time.time()
        ch_len = int(self.chunk_length_s if chunk_length_s is None else chunk_length_s)
        if ch_len <= 0:
            ch_len = self.chunk_length_s

        if audio_array is not None:
            audio = audio_array.astype(np.float32)
            sr = sample_rate
        else:
            loaded_audio, loaded_sr = librosa.load(str(audio_path), sr=16000, mono=True)
            audio = loaded_audio
            sr = int(loaded_sr)

        lang_code = self.supported_languages.get(language.lower(), language)
        if lang_code not in set(self.supported_languages.values()):
            lang_code = "fr"

        segments = self._transcribe_audio(audio, sr, ch_len, lang_code, progress_callback)
        elapsed = time.time() - started
        self._last_transcribe_metadata = {
            "backend": "cohere_tf5",
            "model_name": self.model_name,
            "model_path": self.model_path,
            "tf5_site": self.tf5_site,
            "language": lang_code,
            "chunk_length_s": ch_len,
            "batch_size": self.batch_size,
            "max_new_tokens": self.max_new_tokens,
            "punctuation": self.punctuation,
            "repetition_penalty": self.repetition_penalty,
            "no_repeat_ngram_size": self.no_repeat_ngram_size,
            "segments": len(segments),
            "elapsed_s": round(elapsed, 3),
        }
        return segments

    def transcribe_prechunked(
        self,
        chunks: list[dict],
        language: str = "fr",
        speaker_mapping: dict | None = None,
        progress_callback=None,
    ) -> list[dict]:
        """Transcrit une liste de chunks pyannote en batch.

        Cette voie évite un appel `generate()` par tour et préserve le gain
        observé en bench sur les réunions longues.
        """
        if not self.load():
            return [{"error": "Cohere TF5 non disponible"}]

        started = time.time()
        lang_code = self.supported_languages.get(language.lower(), language)
        if lang_code not in set(self.supported_languages.values()):
            lang_code = "fr"

        segments = [seg for seg in self._run_worker(chunks, lang_code, speaker_mapping) if not seg.get("error")]
        if progress_callback:
            progress_callback(1.0)

        elapsed = time.time() - started
        self._last_transcribe_metadata = {
            "backend": "cohere_tf5",
            "model_name": self.model_name,
            "model_path": self.model_path,
            "tf5_site": self.tf5_site,
            "language": lang_code,
            "chunking_mode": "pyannote_turns_batched",
            "chunks": len(chunks),
            "batch_size": self.batch_size,
            "max_new_tokens": self.max_new_tokens,
            "punctuation": self.punctuation,
            "repetition_penalty": self.repetition_penalty,
            "no_repeat_ngram_size": self.no_repeat_ngram_size,
            "segments": len(segments),
            "elapsed_s": round(elapsed, 3),
        }
        return segments

    def _transcribe_audio(self, audio, sample_rate: int, chunk_length_s: int, language: str, progress_callback=None) -> list[dict]:
        chunk_samples = max(1, int(chunk_length_s * sample_rate))
        chunks = []
        total_samples = len(audio)
        for start_sample in range(0, total_samples, chunk_samples):
            end_sample = min(start_sample + chunk_samples, total_samples)
            if end_sample - start_sample < int(0.5 * sample_rate):
                continue
            chunks.append((start_sample / sample_rate, end_sample / sample_rate, audio[start_sample:end_sample]))

        request_chunks = [
            {"start": round(start_s, 3), "end": round(end_s, 3), "speaker": None, "audio": chunk_audio}
            for start_s, end_s, chunk_audio in chunks
        ]
        segments = self._run_worker(request_chunks, language, speaker_mapping=None)
        if progress_callback:
            progress_callback(1.0)
        return segments

    def _run_worker(self, chunks: list[dict], language: str, speaker_mapping: dict | None) -> list[dict]:
        import numpy as np

        model_path = self._resolve_model_id(self.model_path)
        with tempfile.TemporaryDirectory(prefix="transcria-cohere-tf5-") as tmp:
            tmp_dir = Path(tmp)
            arrays_path = tmp_dir / "chunks.npz"
            input_path = tmp_dir / "request.json"
            output_path = tmp_dir / "response.json"
            arrays: dict[str, object] = {}
            chunk_meta = []
            for index, chunk in enumerate(chunks):
                key = f"chunk_{index}"
                arrays[key] = np.asarray(chunk["audio"], dtype=np.float32)
                raw_speaker = chunk.get("speaker")
                chunk_meta.append({
                    "array_key": key,
                    "start": round(float(chunk["start"]), 3),
                    "end": round(float(chunk["end"]), 3),
                    "speaker": (speaker_mapping or {}).get(raw_speaker, raw_speaker),
                })
            savez = cast(Any, np.savez)
            savez(arrays_path, **arrays)
            input_path.write_text(json.dumps({
                "arrays_path": str(arrays_path),
                "chunks": chunk_meta,
                "config": {
                    "model_path": model_path,
                    "model_revision": self.model_revision,
                    "device": self.device,
                    "language": language,
                    "punctuation": self.punctuation,
                    "batch_size": self.batch_size,
                    "max_new_tokens": self.max_new_tokens,
                    "repetition_penalty": self.repetition_penalty,
                    "no_repeat_ngram_size": self.no_repeat_ngram_size,
                },
            }), encoding="utf-8")
            env = os.environ.copy()
            if self.tf5_site:
                existing = env.get("PYTHONPATH")
                env["PYTHONPATH"] = os.path.abspath(self.tf5_site) if not existing else f"{os.path.abspath(self.tf5_site)}:{existing}"
            proc = subprocess.run(
                [sys.executable, "-m", "transcria.stt._cohere_tf5_worker", "--input", str(input_path), "--output", str(output_path)],
                cwd=os.getcwd(),
                env=env,
                text=True,
                capture_output=True,
                timeout=self.timeout_s,
                check=False,
            )
            if proc.returncode != 0:
                logger.warning(
                    "Cohere TF5 worker échec rc=%s stdout=%s stderr=%s",
                    proc.returncode,
                    proc.stdout[-1000:],
                    proc.stderr[-2000:],
                )
                return [{"error": "Cohere TF5 worker non disponible"}]
            data = json.loads(output_path.read_text(encoding="utf-8"))
        segments = []
        for segment in data.get("segments", []):
            text_raw = str(segment.get("text") or "").strip()
            item = {
                "start": segment["start"],
                "end": segment["end"],
                "text": text_raw,
            }
            if segment.get("speaker"):
                item["speaker"] = segment["speaker"]
            if text_raw:
                text_clean, loops = self._apply_loop_collapse(text_raw)
                item["text"] = text_clean
                if loops:
                    item["hallucination_loops"] = loops
                    item["text_before_loop_collapse"] = text_raw
            segments.append(item)
        return segments

    def _apply_loop_collapse(self, text: str) -> tuple[str, list[dict]]:
        if not self.collapse_repetition_loops:
            return text, []
        from transcria.stt.anti_hallucination import collapse_repetition_loops
        return collapse_repetition_loops(
            text,
            min_repeats=self.repetition_loop_min_repeats,
            max_phrase_words=self.repetition_loop_max_phrase_words,
            keep_repeats=self.repetition_loop_keep_repeats,
        )

    def get_metadata(self) -> dict:
        return dict(self._last_transcribe_metadata)

    def offload(self) -> None:
        import gc

        self._last_transcribe_metadata = {}
        gc.collect()

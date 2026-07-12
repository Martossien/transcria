"""Backend STT expérimental MOSS-Transcribe-Diarize (OpenMOSS, Apache-2.0, 0,9B).

Transcription + étiquettes locuteur + timestamps fins EN UNE PASSE
(``[t][Sxx]texte[t]``) — meilleur WER texte de notre banc de réunions réelles
(cf. docs/STT_BENCHMARK_REAL_MEETINGS.md), la SEULE LLM audio unifiée qui
survit aux fenêtres de 5 minutes sans boucler. Son vice mesuré : l'omission
silencieuse (un saut de ~22 s observé une fois sur huit fenêtres) — d'où le
garde-fou de trous inter-segments ci-dessous (on SIGNALE, on ne supprime ni
n'invente jamais).

Le modèle exige Transformers 5.x : comme ``cohere_tf5``, l'inférence tourne
dans un worker subprocess dont le PYTHONPATH pointe d'abord vers un
site-packages isolé (``moss.moss_site``, créé par ``pip install --target``)
contenant transformers>=5 et le paquet ``moss_transcribe_diarize`` — le venv
projet (Transformers 4.x) reste intact. torch vient du venv.

Les étiquettes locuteur du modèle sont exposées en ``moss_speaker`` (métadonnée,
sans conflit avec l'attribution pyannote du pipeline) ; en transcription de
fichier entier elles sont aussi posées en ``speaker``.
"""
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from transcria.stt.base_transcriber import BaseTranscriber

if TYPE_CHECKING:
    import numpy

logger = logging.getLogger(__name__)

_MOSS_MODEL_REPO = "OpenMOSS-Team/MOSS-Transcribe-Diarize"
# Le modèle annonce 50+ langues, transcrit dans la langue source et n'expose
# AUCUN forçage de langue (le prompt d'entraînement est fixe) : cette table ne
# sert qu'à normaliser la métadonnée de langue du job.
_SUPPORTED_LANGUAGES = {
    "english": "en", "french": "fr", "german": "de", "spanish": "es",
    "italian": "it", "portuguese": "pt", "dutch": "nl", "chinese": "zh",
    "japanese": "ja", "korean": "ko",
}


class MossTranscriber(BaseTranscriber):
    """Backend MOSS-TD 0,9B : worker Transformers 5 isolé, une passe ASR+diar."""

    vram_mb = 4000
    supported_languages = _SUPPORTED_LANGUAGES
    model_name = "moss-transcribe-diarize"
    concurrent_safe = False

    def __init__(
        self,
        model_path: str | None = None,
        device: str | None = None,
        moss_site: str | None = None,
        timeout_s: int = 7200,
        max_new_tokens: int = 8192,
        gap_alert_s: float = 10.0,
        collapse_repetition_loops: bool = True,
        repetition_loop_min_repeats: int = 4,
        repetition_loop_max_phrase_words: int = 10,
        repetition_loop_keep_repeats: int = 2,
    ):
        self.model_path = model_path or _MOSS_MODEL_REPO
        self.device = device or self._detect_device()
        self.moss_site = moss_site
        self.timeout_s = int(timeout_s or 7200)
        self.max_new_tokens = int(max_new_tokens or 8192)
        self.gap_alert_s = float(gap_alert_s)
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
        return bool(self.moss_site) and os.path.isdir(os.path.abspath(str(self.moss_site)))

    def load(self) -> bool:
        if not self.available:
            logger.warning(
                "MOSS STT indisponible: moss_site absent (%s) — créer avec "
                "pip install --target <dir> 'transformers>=5,<6' "
                "git+https://github.com/OpenMOSS/MOSS-Transcribe-Diarize.git",
                self.moss_site,
            )
            return False
        return True

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
            return [{"error": "MOSS STT non disponible (moss_site absent)"}]

        started = time.time()
        whole_file = audio_array is None
        with tempfile.TemporaryDirectory(prefix="transcria-moss-") as tmp:
            tmp_dir = Path(tmp)
            if whole_file:
                if audio_path is None:
                    return [{"error": "MOSS STT: audio_path ou audio_array requis"}]
                wav_path = Path(audio_path)
                duration_s = None
            else:
                import numpy as np
                import soundfile as sf

                array = np.asarray(audio_array, dtype=np.float32)
                wav_path = tmp_dir / "chunk.wav"
                sf.write(str(wav_path), array, int(sample_rate or 16000))
                duration_s = len(array) / float(sample_rate or 16000)
            segments, raw_gaps = self._run_worker(wav_path, tmp_dir, keep_speakers=whole_file)

        if progress_callback:
            progress_callback(1.0)
        elapsed = time.time() - started
        self._last_transcribe_metadata = {
            "backend": "moss",
            "model_name": self.model_name,
            "model_path": self.model_path,
            "moss_site": self.moss_site,
            "language": self._language_code(language),
            "whole_file": whole_file,
            "audio_duration_s": duration_s,
            "max_new_tokens": self.max_new_tokens,
            "segments": len(segments),
            "transcription_gaps": raw_gaps,
            "elapsed_s": round(elapsed, 3),
        }
        if raw_gaps:
            logger.warning(
                "MOSS STT: %d trou(x) inter-segments > %.0fs détecté(s) (%s) — "
                "risque d'omission silencieuse, segments SIGNALÉS non modifiés",
                len(raw_gaps), self.gap_alert_s, raw_gaps,
            )
        return segments

    def transcribe_prechunked(
        self,
        chunks: list[dict],
        language: str = "fr",
        speaker_mapping: dict | None = None,
        progress_callback=None,
    ) -> list[dict]:
        """Transcrit les tours pyannote en UNE invocation worker (modèle chargé
        une fois) — même voie batchée que cohere_tf5. Le locuteur du tour est
        conservé ; la garde de trous ne s'applique pas (les tours sont déjà
        des îlots de parole)."""
        if not self.load():
            return [{"error": "MOSS STT non disponible (moss_site absent)"}]

        import numpy as np

        started = time.time()
        with tempfile.TemporaryDirectory(prefix="transcria-moss-") as tmp:
            tmp_dir = Path(tmp)
            arrays: dict = {}
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
            arrays_path = tmp_dir / "chunks.npz"
            np.savez(arrays_path, **arrays)
            segments, _ = self._run_worker(
                None, tmp_dir, keep_speakers=True,
                extra_payload={"arrays_path": str(arrays_path), "chunks": chunk_meta},
                detect_gaps=False,
            )
        if progress_callback:
            progress_callback(1.0)
        segments = [seg for seg in segments if not seg.get("error")]
        self._last_transcribe_metadata = {
            "backend": "moss",
            "model_name": self.model_name,
            "model_path": self.model_path,
            "moss_site": self.moss_site,
            "language": self._language_code(language),
            "chunking_mode": "pyannote_turns_batched",
            "chunks": len(chunks),
            "max_new_tokens": self.max_new_tokens,
            "segments": len(segments),
            "elapsed_s": round(time.time() - started, 3),
        }
        return segments

    def _run_worker(
        self,
        wav_path: Path | None,
        tmp_dir: Path,
        *,
        keep_speakers: bool,
        extra_payload: dict | None = None,
        detect_gaps: bool = True,
    ) -> tuple[list[dict], list[dict]]:
        input_path = tmp_dir / "request.json"
        output_path = tmp_dir / "response.json"
        payload: dict = {
            "config": {
                "model_path": self.model_path,
                "device": self.device,
                "max_new_tokens": self.max_new_tokens,
            },
        }
        if wav_path is not None:
            payload["audio_path"] = str(wav_path)
        if extra_payload:
            payload.update(extra_payload)
        input_path.write_text(json.dumps(payload), encoding="utf-8")
        env = os.environ.copy()
        if self.moss_site:
            site = os.path.abspath(self.moss_site)
            existing = env.get("PYTHONPATH")
            env["PYTHONPATH"] = site if not existing else f"{site}:{existing}"
        proc = subprocess.run(
            [sys.executable, "-m", "transcria.stt._moss_worker",
             "--input", str(input_path), "--output", str(output_path)],
            cwd=os.getcwd(), env=env, text=True, capture_output=True,
            timeout=self.timeout_s, check=False,
        )
        if proc.returncode != 0:
            logger.warning(
                "MOSS worker échec rc=%s stdout=%s stderr=%s",
                proc.returncode, proc.stdout[-1000:], proc.stderr[-2000:],
            )
            return [{"error": "MOSS worker non disponible"}], []
        data = json.loads(output_path.read_text(encoding="utf-8"))

        segments: list[dict] = []
        for seg in data.get("segments", []):
            text_raw = str(seg.get("text") or "").strip()
            if not text_raw:
                continue
            item: dict = {"start": seg["start"], "end": seg["end"], "text": text_raw, "backend": "moss"}
            if seg.get("speaker"):
                item["moss_speaker"] = seg["speaker"]
                if keep_speakers:
                    item["speaker"] = seg["speaker"]
            if self.collapse_repetition_loops:
                text_clean, loops = self._apply_loop_collapse(text_raw)
                item["text"] = text_clean
                if loops:
                    item["hallucination_loops"] = loops
                    item["text_before_loop_collapse"] = text_raw
            segments.append(item)
        return segments, (self._detect_gaps(segments) if detect_gaps else [])

    def _detect_gaps(self, segments: list[dict]) -> list[dict]:
        """Trous inter-segments > gap_alert_s : signature de l'omission silencieuse
        de MOSS (parole sautée SANS anomalie visible — timestamps monotones).
        On marque le segment aval + on remonte la liste en métadonnées ; on ne
        supprime ni ne fabrique jamais rien."""
        if self.gap_alert_s <= 0:
            return []
        gaps: list[dict] = []
        for prev, cur in zip(segments, segments[1:]):
            gap = float(cur["start"]) - float(prev["end"])
            if gap > self.gap_alert_s:
                cur["transcription_gap_before_s"] = round(gap, 1)
                gaps.append({"from": prev["end"], "to": cur["start"], "gap_s": round(gap, 1)})
        return gaps

    def _language_code(self, language: str) -> str:
        lang = str(language or "fr").strip().lower()
        if lang in self.supported_languages:
            return self.supported_languages[lang]
        if lang in self.supported_languages.values():
            return lang
        return lang or "fr"

    def _apply_loop_collapse(self, text: str) -> tuple[str, list[dict]]:
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

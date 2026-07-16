"""Backend STT Kroko-ASR (Banafo) — zipformer2 streaming par langue sur sherpa-onnx, CPU.

Seul backend qui tourne SANS GPU : les modèles community (CC-BY-SA, ~155 Mo par langue)
atteignent sur notre corpus de réunions réelles le niveau des gros modèles GPU
(cf. docs/STT_BENCHMARK_REAL_MEETINGS.md). Un modèle PAR langue — la langue du job
choisit le fichier. Comme les autres backends ASR purs, la diarisation reste portée
par pyannote/Sortformer dans le pipeline.

Les poids sont publiés dans un conteneur ``.data`` maison : blocs préfixés par leur
longueur (uint32 LE) — en-tête JSON puis encoder/decoder/joiner ONNX puis tokens.txt,
c'est-à-dire le quatuor transducer sherpa-onnx standard. On extrait au premier
chargement dans ``model_dir`` (le ``.data`` vient du cache HF — page « Modèles » —
ou est téléchargé à la demande).
"""
import json
import logging
import struct
import time as _time
from pathlib import Path
from typing import Any

from transcria.config.loader import _deep_merge, get_default_config
from transcria.stt.anti_hallucination import collapse_repetition_loops
from transcria.stt.base_transcriber import BaseTranscriber
from transcria.stt.registry import ModelCatalogEntry, SttBackendDescriptor

logger = logging.getLogger(__name__)

_KROKO_REPO = "Banafo/Kroko-ASR"
# Langues des modèles community publiés (ISO du nom de fichier ; « iw » = hébreu, code legacy).
_SUPPORTED_LANGUAGES = {
    "english": "en",
    "french": "fr",
    "german": "de",
    "spanish": "es",
    "italian": "it",
    "portuguese": "pt",
    "dutch": "nl",
    "turkish": "tr",
    "swedish": "sv",
    "hebrew": "iw",
}
_CONTAINER_MEMBERS = ("header.json", "encoder.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt")
_MODEL_MEMBERS = _CONTAINER_MEMBERS[1:]


class KrokoContainerError(RuntimeError):
    """Conteneur .data illisible ou incohérent (tronqué, mauvais type…)."""


def data_filename(lang_code: str, variant: str) -> str:
    return f"Kroko-{lang_code.upper()}-Community-{variant}-L-Streaming-001.data"


def extract_container(data_path: Path, out_dir: Path) -> dict:
    """Extrait le conteneur .data (blocs [len u32 LE][payload]) vers ``out_dir``.

    Retourne l'en-tête JSON. Idempotent : si les 4 fichiers modèle sont déjà là,
    ne réécrit rien.
    """
    if all((out_dir / name).exists() for name in _MODEL_MEMBERS):
        header_file = out_dir / "header.json"
        if header_file.exists():
            return json.loads(header_file.read_text(encoding="utf-8"))
        return {}
    out_dir.mkdir(parents=True, exist_ok=True)
    header: dict = {}
    with open(data_path, "rb") as fh:
        size = fh.seek(0, 2)
        fh.seek(0)
        for name in _CONTAINER_MEMBERS:
            raw = fh.read(4)
            if len(raw) < 4:
                raise KrokoContainerError(f"conteneur tronqué avant {name}: {data_path}")
            length = struct.unpack("<I", raw)[0]
            if fh.tell() + length > size:
                raise KrokoContainerError(f"bloc {name} dépasse le fichier: {data_path}")
            payload = fh.read(length)
            if name == "header.json":
                try:
                    header = json.loads(payload)
                except ValueError as exc:
                    raise KrokoContainerError(f"en-tête JSON invalide: {data_path}") from exc
                if header.get("type") != "zipformer2":
                    raise KrokoContainerError(
                        f"type de modèle inattendu '{header.get('type')}' (zipformer2 attendu): {data_path}"
                    )
            (out_dir / name).write_bytes(payload)
    return header


class KrokoTranscriber(BaseTranscriber):
    """Backend Kroko-ASR community (CC-BY-SA) : zipformer2 streaming, CPU pur."""

    vram_mb = 0  # CPU uniquement — aucune réservation GPU (cf. get_backend_vram_mb)
    supported_languages = _SUPPORTED_LANGUAGES
    model_name = "kroko-community"

    def __init__(
        self,
        model_dir: str | None = None,
        repo_id: str | None = None,
        variant: str = "128",
        device: str | None = None,  # accepté pour le contrat factory, ignoré (CPU)
        num_threads: int = 8,
        decoding_method: str = "greedy_search",
        tail_padding_s: float = 0.66,
        segment_max_gap_s: float = 0.8,
        segment_max_len_s: float = 15.0,
        collapse_repetition_loops: bool = True,
        repetition_loop_min_repeats: int = 4,
        repetition_loop_max_phrase_words: int = 10,
        repetition_loop_keep_repeats: int = 2,
    ):
        self.model_dir = Path(model_dir or "./models/kroko")
        self.repo_id = repo_id or _KROKO_REPO
        self.variant = str(variant or "128")
        self.num_threads = int(num_threads or 8)
        self.decoding_method = str(decoding_method or "greedy_search")
        self.tail_padding_s = float(tail_padding_s)
        self.segment_max_gap_s = float(segment_max_gap_s)
        self.segment_max_len_s = float(segment_max_len_s)
        self.collapse_repetition_loops = collapse_repetition_loops
        self.repetition_loop_min_repeats = repetition_loop_min_repeats
        self.repetition_loop_max_phrase_words = repetition_loop_max_phrase_words
        self.repetition_loop_keep_repeats = repetition_loop_keep_repeats
        self._recognizers: dict[str, Any] = {}
        self._metadata: dict = {
            "backend": "kroko",
            "repo_id": self.repo_id,
            "variant": self.variant,
            "device": "cpu",
            "calls": 0,
            "segments": 0,
            "elapsed_s": 0.0,
        }

    @property
    def available(self) -> bool:
        try:
            import sherpa_onnx  # noqa: F401
        except ImportError:
            logger.warning("Kroko STT indisponible: sherpa-onnx absent (pip install sherpa-onnx)")
            return False
        return True

    def load(self) -> bool:
        # Les modèles sont PAR langue : le chargement réel est différé au premier
        # transcribe() (la langue n'est pas connue ici). On valide juste le runtime.
        return self.available

    def offload(self) -> None:
        self._recognizers = {}

    def get_metadata(self) -> dict:
        return dict(self._metadata)

    def transcribe(
        self,
        audio_path: Path | None,
        language: str = "fr",
        chunk_length_s: int | None = None,
        progress_callback=None,
        audio_array=None,
        sample_rate: int = 16000,
    ) -> list[dict]:
        if not self.available:
            return [{"error": "Kroko STT non disponible (sherpa-onnx absent)"}]

        import numpy as np

        t0 = _time.time()
        if audio_array is not None:
            audio = np.asarray(audio_array, dtype=np.float32)
            sr = int(sample_rate or 16000)
            source = "audio_array"
        else:
            if audio_path is None:
                return [{"error": "Kroko STT: audio_path ou audio_array requis"}]
            import librosa

            source = str(audio_path)
            audio, _sr = librosa.load(str(audio_path), sr=16000, mono=True)
            sr = int(_sr)
        if sr != 16000:
            import librosa

            audio = librosa.resample(audio, orig_sr=sr, target_sr=16000)
            sr = 16000
        if len(audio) == 0:
            return []

        lang_code = self._language_code(language)
        try:
            recognizer = self._recognizer_for(lang_code)
        except Exception as exc:  # noqa: BLE001 — modèle absent/corrompu ⇒ erreur propre, pas un crash
            logger.warning("Kroko STT: chargement %s impossible: %s", lang_code, exc)
            return [{"error": f"Kroko STT: modèle {lang_code} indisponible ({exc})"}]

        total_duration = len(audio) / sr
        logger.info(
            "Transcription Kroko: source=%s langue=%s durée=%.1fs variant=%s (CPU, %d threads)",
            source, lang_code, total_duration, self.variant, self.num_threads,
        )

        # Flux streaming : on alimente par tranches de 10 s en décodant au fil de
        # l'eau (mémoire bornée sur les gros fichiers), puis padding de queue pour
        # vider le contexte droit du zipformer.
        stream = recognizer.create_stream()
        feed_samples = sr * 10
        for start in range(0, len(audio), feed_samples):
            stream.accept_waveform(sr, audio[start:start + feed_samples])
            while recognizer.is_ready(stream):
                recognizer.decode_stream(stream)
            if progress_callback and total_duration > 0:
                progress_callback(min((start + feed_samples) / len(audio), 1.0))
        if self.tail_padding_s > 0:
            stream.accept_waveform(sr, np.zeros(int(sr * self.tail_padding_s), dtype=np.float32))
        stream.input_finished()
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)

        result = recognizer.get_result_all(stream)
        tokens = list(getattr(result, "tokens", []) or [])
        timestamps = [float(ts) for ts in (getattr(result, "timestamps", []) or [])]
        segments = self._segments_from_tokens(tokens, timestamps, total_duration)
        if not segments:
            text = str(getattr(result, "text", "") or "").strip()
            if text:
                segments = [{"start": 0.0, "end": round(total_duration, 3), "text": text}]

        out: list[dict] = []
        for seg in segments:
            text = seg["text"]
            loops: list = []
            if text and self.collapse_repetition_loops:
                text, loops = self._apply_loop_collapse(text)
            if not text:
                continue
            item = {"start": seg["start"], "end": seg["end"], "text": text, "backend": "kroko"}
            if loops:
                item["hallucination_loops"] = loops
            out.append(item)

        elapsed = _time.time() - t0
        self._metadata["calls"] = int(self._metadata.get("calls", 0)) + 1
        self._metadata["segments"] = int(self._metadata.get("segments", 0)) + len(out)
        self._metadata["elapsed_s"] = round(float(self._metadata.get("elapsed_s", 0.0)) + elapsed, 3)
        self._metadata["last_audio_duration_s"] = round(total_duration, 3)
        logger.info("Transcription Kroko terminée: %d segments en %.1fs", len(out), elapsed)
        return out

    # ── Résolution modèle / chargement ────────────────────────────────────────

    def _recognizer_for(self, lang_code: str):
        recognizer = self._recognizers.get(lang_code)
        if recognizer is not None:
            return recognizer
        import sherpa_onnx

        extracted = self._ensure_extracted(lang_code)
        load_t0 = _time.time()
        recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(extracted / "tokens.txt"),
            encoder=str(extracted / "encoder.onnx"),
            decoder=str(extracted / "decoder.onnx"),
            joiner=str(extracted / "joiner.onnx"),
            num_threads=self.num_threads,
            provider="cpu",
            sample_rate=16000,
            feature_dim=80,
            decoding_method=self.decoding_method,
        )
        self._recognizers[lang_code] = recognizer
        logger.info(
            "Kroko STT chargé: langue=%s dossier=%s durée=%.2fs",
            lang_code, extracted, _time.time() - load_t0,
        )
        return recognizer

    def _ensure_extracted(self, lang_code: str) -> Path:
        """Dossier extrait pour cette langue (extraction du .data si nécessaire)."""
        for variant in self._variant_candidates():
            out_dir = self.model_dir / f"{lang_code}{variant}"
            if all((out_dir / name).exists() for name in _MODEL_MEMBERS):
                return out_dir
        data_path, variant = self._resolve_data_path(lang_code)
        out_dir = self.model_dir / f"{lang_code}{variant}"
        extract_container(data_path, out_dir)
        return out_dir

    def _variant_candidates(self) -> list[str]:
        # Repli 128 → 64 : certaines langues (ex. suédois) n'existent qu'en 64.
        return [self.variant] + [v for v in ("128", "64") if v != self.variant]

    def _resolve_data_path(self, lang_code: str) -> tuple[Path, str]:
        """Cherche le .data : dossier local, puis cache HF, puis téléchargement HF."""
        for variant in self._variant_candidates():
            filename = data_filename(lang_code, variant)
            local = self.model_dir / filename
            if local.exists():
                return local, variant
        try:
            from huggingface_hub import try_to_load_from_cache

            for variant in self._variant_candidates():
                cached = try_to_load_from_cache(self.repo_id, data_filename(lang_code, variant))
                if isinstance(cached, str):
                    return Path(cached), variant
        except ImportError:
            pass
        from huggingface_hub import hf_hub_download

        last_exc: Exception | None = None
        for variant in self._variant_candidates():
            filename = data_filename(lang_code, variant)
            try:
                logger.info("Kroko STT: téléchargement %s depuis %s", filename, self.repo_id)
                return Path(hf_hub_download(repo_id=self.repo_id, filename=filename)), variant
            except Exception as exc:  # noqa: BLE001 — variante absente ⇒ on tente la suivante
                last_exc = exc
        raise KrokoContainerError(
            f"aucun modèle Kroko pour '{lang_code}' (variantes {self._variant_candidates()}): {last_exc}"
        )

    # ── Segmentation / texte ──────────────────────────────────────────────────

    def _segments_from_tokens(
        self, tokens: list[str], timestamps: list[float], total_duration: float
    ) -> list[dict]:
        """Groupe les tokens horodatés en segments : coupe sur un silence
        > segment_max_gap_s ou une durée > segment_max_len_s."""
        if not tokens or len(tokens) != len(timestamps):
            return []
        segments: list[dict] = []
        seg_tokens: list[str] = [tokens[0]]
        seg_start = timestamps[0]
        prev_ts = timestamps[0]
        for token, ts in zip(tokens[1:], timestamps[1:]):
            if (ts - prev_ts) > self.segment_max_gap_s or (ts - seg_start) > self.segment_max_len_s:
                self._flush_segment(segments, seg_tokens, seg_start, prev_ts)
                seg_tokens = [token]
                seg_start = ts
            else:
                seg_tokens.append(token)
            prev_ts = ts
        end = min(prev_ts + 0.3, total_duration) if total_duration else prev_ts + 0.3
        self._flush_segment(segments, seg_tokens, seg_start, end)
        return segments

    @staticmethod
    def _flush_segment(segments: list[dict], seg_tokens: list[str], start: float, end: float) -> None:
        text = "".join(seg_tokens).strip()
        if text:
            segments.append({
                "start": round(float(start), 3),
                "end": round(float(max(end, start + 0.1)), 3),
                "text": text,
            })

    def _language_code(self, language: str) -> str:
        lang = str(language or "fr").strip().lower()
        if lang in self.supported_languages:
            return self.supported_languages[lang]
        if lang in self.supported_languages.values():
            return lang
        if lang in {"he", "heb"}:  # code hébreu moderne → code legacy des fichiers Kroko
            return "iw"
        logger.warning("Kroko STT: langue '%s' non supportée, repli fr", language)
        return "fr"

    def _apply_loop_collapse(self, text: str) -> tuple[str, list[dict]]:
        return collapse_repetition_loops(
            text,
            min_repeats=self.repetition_loop_min_repeats,
            max_phrase_words=self.repetition_loop_max_phrase_words,
            keep_repeats=self.repetition_loop_keep_repeats,
        )


# --- Enregistrement au registre STT (vague C1) --------------------------------

def _effective_kroko_config(config: dict) -> dict:
    current = config.get("kroko", {})
    defaults = get_default_config()["kroko"]
    return _deep_merge(defaults, current)


def build(config: dict, device: str | None = None) -> KrokoTranscriber:
    kroko_cfg = _effective_kroko_config(config)
    return KrokoTranscriber(
        model_dir=kroko_cfg.get("model_dir"),
        repo_id=kroko_cfg.get("repo_id"),
        variant=kroko_cfg.get("variant", "128"),
        device=device,  # ignoré (CPU pur) — gardé pour le contrat commun
        num_threads=kroko_cfg.get("num_threads", 8),
        decoding_method=kroko_cfg.get("decoding_method", "greedy_search"),
        tail_padding_s=kroko_cfg.get("tail_padding_s", 0.66),
        segment_max_gap_s=kroko_cfg.get("segment_max_gap_s", 0.8),
        segment_max_len_s=kroko_cfg.get("segment_max_len_s", 15.0),
        collapse_repetition_loops=kroko_cfg.get("collapse_repetition_loops", True),
        repetition_loop_min_repeats=kroko_cfg.get("repetition_loop_min_repeats", 4),
        repetition_loop_max_phrase_words=kroko_cfg.get("repetition_loop_max_phrase_words", 10),
        repetition_loop_keep_repeats=kroko_cfg.get("repetition_loop_keep_repeats", 2),
    )


def vram_mb(config: dict) -> int:
    return 0  # CPU pur (sherpa-onnx) — aucune VRAM, aucune réservation GPU


DESCRIPTOR = SttBackendDescriptor(
    name="kroko",
    build=build,
    vram_mb=vram_mb,
    catalog=ModelCatalogEntry(
        # Snapshot complet = les 10 langues community (~155 Mo chacune) ; seul backend CPU (sans GPU).
        repo="Banafo/Kroko-ASR",
        gated=False,
        license="CC-BY-SA (community)",
        license_url="https://huggingface.co/Banafo/Kroko-ASR",
        est_gb=3.2,
    ),
)

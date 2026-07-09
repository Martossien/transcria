import logging
import re
import time
from pathlib import Path

import numpy as np

from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job
from transcria.logging_setup import get_structured_logger
from transcria.stt.transcriber_factory import create_transcriber

logger = logging.getLogger(__name__)

_SR = 16000
_DEFAULT_SUBTITLE_ARTIFACT_PATTERNS = [
    # --- Marqueurs diffusion Radio-Canada ---
    re.compile(r"\bsous\s*-?\s*titrage\b", re.IGNORECASE),
    re.compile(r"\bst['\s]*501\b", re.IGNORECASE),
    re.compile(r"\bfr\s*2021\b", re.IGNORECASE),
    re.compile(r"\bsoci[eé]t[eé]\s+radio(?:-canada)?\b", re.IGNORECASE),
    # --- Outros YouTube / contenus viraux (anglais) ---
    # Ces phrases n'apparaissent jamais dans un contexte de réunion professionnelle.
    re.compile(r"\bthanks?\s+for\s+watching\b", re.IGNORECASE),
    re.compile(r"\bthank\s+you\s+for\s+watching\b", re.IGNORECASE),
    re.compile(r"\bdon'?t\s+forget\s+to\s+subscribe\b", re.IGNORECASE),
    # --- Crédits sous-titrage et services tiers ---
    re.compile(r"\bsubtitles\s+by\s+the\s+amara\b", re.IGNORECASE),
    re.compile(r"\btranscription\s+by\s+castingwords\b", re.IGNORECASE),
]
_DEFAULT_SHORT_SUBTITLE_ARTIFACTS = {
    # Marqueurs courts Radio-Canada
    "sous",
    "titrage",
    "-titrage",
    "fr",
    "fr?",
    "st",
    "st501",
    "st 501",
    "st'501",
    "st' 501",
    "titrage fr",
    "titrage st",
    "titrage st'",
    "titrage st 501",
    "titrage st' 501",
    # Appels à l'abonnement YouTube (exact match normalisé)
    "thanks for watching",
    "thank you for watching",
    "thank you for watching please subscribe",
    "please subscribe to my channel",
    "like and subscribe",
    "please like and subscribe",
    "don't forget to subscribe",
}
_DEFAULT_ISOLATED_NOISE_ARTIFACTS = {
    "501",
}
_DEFAULT_NON_LATIN_CHAR_PATTERN = (
    r"[\u0400-\u04FF\u0600-\u06FF\u0750-\u077F"
    r"\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF]"
)
_DEFAULT_GENERIC_HALLUCINATION_PATTERNS = [
    # Segments isolés observés sur silences/bruits de réunions françaises.
    re.compile(r"^\s*thank\s+you\s*[.!?…]*\s*$", re.IGNORECASE),
    re.compile(r"^\s*thank\s+you\s+very\s+much\s*[.!?…]*\s*$", re.IGNORECASE),
    re.compile(r"^\s*thanks\s*[.!?…]*\s*$", re.IGNORECASE),
    re.compile(r"^\s*bye\s*[.!?…]*\s*$", re.IGNORECASE),
    re.compile(r"^\s*i\s+got\s+it\s*[.!?…]*\s*$", re.IGNORECASE),
    re.compile(r"^\s*merci\s+d['’]avoir\s+regard[ée]\s+cette\s+vid[ée]o\s*[.!?…]*\s*$", re.IGNORECASE),
]


class Transcriber:
    def __init__(self, config: dict, gpu_index: int = 0):
        self.config = config
        device = f"cuda:{gpu_index}" if gpu_index is not None else "cuda:0"
        self.transcriber = create_transcriber(config, device=device)
        self.gpu_index = gpu_index
        self._last_chunk_metrics: dict | None = None

    # ── API publique ──────────────────────────────────────────────────────────

    def transcribe(self, job: Job, audio_path: Path) -> dict:
        import librosa

        fs = JobFilesystem(self.config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
        sl = get_structured_logger(__name__)
        sl.set_context(job_id=job.id, step="transcribe")

        from transcria.gpu.opencode_runner import resolve_output_language
        lang = resolve_output_language(job)
        backend = self.config.get("models", {}).get("stt_backend", "cohere")

        speaker_turns = fs.load_json("speakers/speaker_turns.json")
        speaker_mapping = fs.load_json("speakers/speaker_mapping.json")

        sl.info("DÉBUT transcription", backend=backend, gpu=self.gpu_index)
        self._last_chunk_metrics = None

        from transcria.audio.vad_adaptive import AdaptiveVADConfig

        vad_cfg = self.config.get("workflow", {}).get("vad", {})
        audio_quality = fs.load_json("metadata/audio_quality_decision.json") or {}
        vad_cfg = AdaptiveVADConfig.resolve(vad_cfg, audio_quality)
        vad_enabled = self._resolve_final_vad_enabled(
            vad_cfg,
            audio_quality,
            self.config.get("workflow", {}),
            sl,
        )

        # Sur audio très faible, pyannote ne détecte souvent qu'un tour court (~5s)
        # ce qui limite la transcription à ~17% de l'audio. Forcer le 30s_fallback
        # pour que les backends traitent l'intégralité du signal.
        preflight = fs.load_json("metadata/audio_preflight.json") or {}
        preflight_flags = preflight.get("flags") or []
        force_30s_on_weak = "audio_tres_faible" in preflight_flags

        # Choisir le mode de chunking selon la disponibilité des exclusive_turns
        if speaker_turns and speaker_turns.get("exclusive_turns") and not force_30s_on_weak:
            # Chunking par tours pyannote : charger l'audio une seule fois en mémoire,
            # passer des numpy arrays à chaque chunk → pas de fichiers WAV temporaires.
            audio, sr = librosa.load(str(audio_path), sr=_SR, mono=True)
            total_duration = len(audio) / sr
            chunk_cfg = self.config.get("workflow", {}).get("pyannote_chunking", {}) or {}
            chunks = self._build_chunks_from_turns(
                audio, total_duration, speaker_turns, chunk_cfg=chunk_cfg
            )
            if chunks:
                if vad_enabled:
                    chunks = self._apply_vad_filter(chunks, audio, sl, vad_cfg)
                sl.info("Mode transcription: tours pyannote (%d chunks)", len(chunks), backend=backend)
                segments = self._transcribe_by_chunks(chunks, lang, speaker_mapping, sl)
                if not segments:
                    sl.info("Aucun segment produit par tours pyannote → fallback 30s fixes", backend=backend)
                    segments = self.transcriber.transcribe(audio_path, language=lang)
                    if speaker_turns and speaker_turns.get("turns"):
                        segments = self._apply_speakers(segments, speaker_turns, speaker_mapping)
                    chunking_mode = "30s_fallback"
                else:
                    chunking_mode = "pyannote_turns"
            else:
                # _build_chunks_from_turns a retourné None (turns vides après filtrage)
                sl.info("Mode transcription: 30s fixes (aucun chunk pyannote valide)", backend=backend)
                segments = self.transcriber.transcribe(audio_path, language=lang)
                if speaker_turns and speaker_turns.get("turns"):
                    segments = self._apply_speakers(segments, speaker_turns, speaker_mapping)
                chunking_mode = "30s_fallback"
        else:
            # Fallback : chunking 30s fixe + overlap matching (pas de chargement librosa ici)
            reason = (
                "audio_tres_faible détecté (tours pyannote peu fiables)"
                if force_30s_on_weak
                else "exclusive_turns absent"
            )
            sl.info("Mode transcription: 30s fixes (%s)", reason, backend=backend)
            segments = self.transcriber.transcribe(audio_path, language=lang)
            if speaker_turns and speaker_turns.get("turns"):
                segments = self._apply_speakers(segments, speaker_turns, speaker_mapping)
            chunking_mode = "30s_fallback"

        segments = self._apply_forced_alignment_if_enabled(
            audio_path, segments, lang, backend, sl
        )
        segments = self._apply_speaker_realignment(
            segments, speaker_turns, speaker_mapping, sl
        )
        segments = self._cleanup_transcription_segments(segments, sl, language=lang)
        segments = self._score_segment_reliability(segments, fs, sl)
        corpus_summary = self._write_stt_corpus(job, segments, backend, fs, sl)
        backend_metadata = self._backend_metadata()
        if backend_metadata:
            fs.save_json(f"metadata/{backend}.json", backend_metadata)

        speaker_map = speaker_mapping or {}
        srt_content = self.transcriber.segments_to_srt(segments, speaker_map.get("mapping"))
        speaker_count = len(set(s.get("speaker", "") for s in segments if s.get("speaker")))
        fs.save_text("metadata/transcription.srt", srt_content)
        fs.save_json("metadata/transcription_segments.json", segments)
        fs.save_json("metadata/transcription_metadata.json", {
            "backend": backend,
            "chunking_mode": chunking_mode,
            "chunking_forced_30s_reason": "audio_tres_faible" if force_30s_on_weak else None,
            "chunk_metrics": self._last_chunk_metrics,
            "gpu_index": self.gpu_index,
            "language": lang,
            "segments": len(segments),
            "speaker_count": speaker_count,
            "vad_final_enabled": vad_enabled,
            "backend_metadata_path": f"metadata/{backend}.json" if backend_metadata else None,
            "difficulty_corpus": corpus_summary,
        })
        fs.save_json("metadata/speakers_map.json", speaker_map)

        sl.info(
            "FIN transcription",
            segments=len(segments),
            speakers=speaker_count,
            srt_chars=len(srt_content),
            backend=backend,
            chunking_mode=chunking_mode,
        )

        return {
            "segments": segments,
            "srt_content": srt_content,
            "speaker_count": speaker_count,
        }

    # ── Réglages qualité post-STT ────────────────────────────────────────────

    @staticmethod
    def _resolve_final_vad_enabled(vad_cfg: dict, audio_quality: dict, workflow_cfg: dict, sl=None) -> bool:
        """Active le VAD final explicite, ou automatiquement sur audio dégradé."""
        fallback = workflow_cfg.get("enable_vad", False)
        if vad_cfg.get("enabled_final", fallback):
            return True

        if not vad_cfg.get("auto_enable_final_on_degraded", True):
            return False

        level = str((audio_quality or {}).get("level") or "").strip()
        levels = set(vad_cfg.get("auto_enable_final_levels") or ["degrade"])
        if level not in levels:
            return False

        auto_threshold = vad_cfg.get("threshold_final_degraded", 0.6)
        if auto_threshold is not None:
            vad_cfg["threshold"] = auto_threshold
        if sl:
            sl.info(
                "VAD final activé automatiquement pour audio dégradé",
                quality_level=level,
                threshold=vad_cfg.get("threshold"),
            )
        return True

    def _cleanup_transcription_segments(self, segments: list[dict], sl=None, language: str | None = None) -> list[dict]:
        """Nettoie les artefacts ASR mesurés et fusionne les micro-segments sûrs."""
        cfg = self.config.get("workflow", {}).get("transcription_cleanup", {}) or {}
        if not cfg.get("enabled", True):
            return segments

        remove_artifacts = cfg.get("remove_subtitle_artifacts", True)
        remove_obvious_hallucinations = cfg.get("remove_obvious_hallucinations", True)
        merge_short = cfg.get("merge_short_segments", True)

        artifact_patterns = self._build_artifact_patterns(cfg)
        artifact_words = self._build_artifact_words(cfg)
        isolated_noise_words = self._build_isolated_noise_words(cfg)
        hallucination_patterns = self._build_generic_hallucination_patterns(cfg)
        non_latin_re = self._build_non_latin_pattern(cfg)

        cleaned: list[dict] = []
        removed_artifacts = 0
        removed_hallucinations = 0
        merged_short = 0

        for segment in segments:
            text = str(segment.get("text") or "").strip()
            if not text:
                continue
            if self._is_punctuation_only_artifact(text):
                removed_artifacts += 1
                continue
            if remove_artifacts and self._is_subtitle_artifact(text, artifact_patterns, artifact_words):
                removed_artifacts += 1
                continue
            if remove_artifacts and self._is_isolated_noise_artifact(text, segment, cfg, isolated_noise_words):
                removed_artifacts += 1
                continue
            if remove_obvious_hallucinations and self._is_obvious_hallucination(
                text,
                cfg,
                hallucination_patterns,
                non_latin_re,
                language=language,
            ):
                removed_hallucinations += 1
                continue

            current = dict(segment)
            current["text"] = text
            if merge_short and cleaned and self._can_merge_short_segment(
                cleaned[-1],
                current,
                cfg,
            ):
                self._merge_segment_into_previous(cleaned[-1], current)
                merged_short += 1
            else:
                cleaned.append(current)

        if sl and (removed_artifacts or removed_hallucinations or merged_short):
            sl.info(
                "Nettoyage transcription appliqué",
                removed_artifacts=removed_artifacts,
                removed_hallucinations=removed_hallucinations,
                merged_short_segments=merged_short,
                segments_before=len(segments),
                segments_after=len(cleaned),
            )
        return cleaned

    @staticmethod
    def _build_artifact_patterns(cfg: dict) -> list:
        """Returns compiled patterns from config if non-empty, else built-in defaults."""
        raw = cfg.get("subtitle_artifact_patterns")
        if raw:
            return [re.compile(p, re.IGNORECASE) for p in raw]
        return _DEFAULT_SUBTITLE_ARTIFACT_PATTERNS

    @staticmethod
    def _build_artifact_words(cfg: dict) -> set:
        """Returns word set from config if non-empty, else built-in defaults."""
        raw = cfg.get("subtitle_artifact_words")
        if raw:
            return set(raw)
        return _DEFAULT_SHORT_SUBTITLE_ARTIFACTS

    @staticmethod
    def _build_isolated_noise_words(cfg: dict) -> set:
        raw = cfg.get("isolated_noise_artifact_words")
        if raw:
            return {Transcriber._normalize_artifact_text(str(item)) for item in raw}
        return _DEFAULT_ISOLATED_NOISE_ARTIFACTS

    @staticmethod
    def _build_generic_hallucination_patterns(cfg: dict) -> list:
        raw = cfg.get("generic_hallucination_patterns")
        if raw:
            return [re.compile(p, re.IGNORECASE) for p in raw]
        return _DEFAULT_GENERIC_HALLUCINATION_PATTERNS

    @staticmethod
    def _build_non_latin_pattern(cfg: dict) -> re.Pattern:
        raw = cfg.get("non_latin_char_pattern") or _DEFAULT_NON_LATIN_CHAR_PATTERN
        return re.compile(str(raw), re.IGNORECASE)

    @staticmethod
    def _is_subtitle_artifact(text: str, patterns: list | None = None, words: set | None = None) -> bool:
        if patterns is None:
            patterns = _DEFAULT_SUBTITLE_ARTIFACT_PATTERNS
        if words is None:
            words = _DEFAULT_SHORT_SUBTITLE_ARTIFACTS
        normalized = Transcriber._normalize_artifact_text(text)
        if normalized in words:
            return True
        if Transcriber._word_count(text) > 6:
            return False
        return any(pattern.search(text) for pattern in patterns)

    @staticmethod
    def _is_punctuation_only_artifact(text: str) -> bool:
        stripped = text.strip()
        if not stripped:
            return True
        return not re.search(r"[^\W_]", stripped, flags=re.UNICODE)

    @staticmethod
    def _is_isolated_noise_artifact(text: str, segment: dict, cfg: dict, words: set) -> bool:
        normalized = Transcriber._normalize_artifact_text(text)
        if normalized not in words:
            return False
        if Transcriber._word_count(text) != 1:
            return False
        duration_s = float(segment.get("end") or 0.0) - float(segment.get("start") or 0.0)
        return duration_s <= float(cfg.get("isolated_noise_artifact_max_s", 0.8))

    @staticmethod
    def _is_obvious_hallucination(
        text: str,
        cfg: dict,
        generic_patterns: list,
        non_latin_re: re.Pattern,
        *,
        language: str | None = None,
    ) -> bool:
        if cfg.get("remove_non_latin_hallucinations", True):
            min_chars = int(cfg.get("non_latin_min_chars", 2))
            min_ratio = float(cfg.get("non_latin_min_ratio", 0.25))
            if Transcriber._is_non_latin_hallucination(text, non_latin_re, min_chars, min_ratio):
                return True

        if cfg.get("remove_generic_hallucinations", True) and Transcriber._language_allows_generic_cleanup(language, cfg):
            return any(pattern.search(text) for pattern in generic_patterns)

        return False

    @staticmethod
    def _is_non_latin_hallucination(text: str, non_latin_re: re.Pattern, min_chars: int, min_ratio: float) -> bool:
        non_latin_chars = non_latin_re.findall(text)
        if len(non_latin_chars) < min_chars:
            return False

        letters = re.findall(r"[^\W\d_]", text, flags=re.UNICODE)
        if not letters:
            return False
        ratio = len(non_latin_chars) / len(letters)
        return ratio >= min_ratio

    @staticmethod
    def _language_allows_generic_cleanup(language: str | None, cfg: dict) -> bool:
        allowed = cfg.get("generic_hallucination_languages") or ["fr"]
        normalized_allowed = {str(item).lower().strip() for item in allowed if str(item).strip()}
        if not normalized_allowed:
            return False
        normalized_language = str(language or cfg.get("expected_language") or "fr").lower().strip()
        root_language = normalized_language.split("-", 1)[0].split("_", 1)[0]
        return normalized_language in normalized_allowed or root_language in normalized_allowed

    @staticmethod
    def _normalize_artifact_text(text: str) -> str:
        lowered = text.lower().strip()
        lowered = lowered.replace("’", "'").replace("`", "'")
        lowered = re.sub(r"[\s\u00a0]+", " ", lowered)
        lowered = re.sub(r"^[\W_]+|[\W_]+$", "", lowered)
        return lowered

    @staticmethod
    def _can_merge_short_segment(previous: dict, current: dict, cfg: dict) -> bool:
        if previous.get("speaker") != current.get("speaker"):
            return False

        prev_end = float(previous.get("end") or 0)
        cur_start = float(current.get("start") or 0)
        gap_s = cur_start - prev_end
        if gap_s < -0.05 or gap_s > float(cfg.get("merge_gap_s", 0.5)):
            return False

        duration_s = float(current.get("end") or 0) - cur_start
        if duration_s > float(cfg.get("short_segment_max_s", 0.45)):
            return False

        words = Transcriber._word_count(current.get("text") or "")
        if words > int(cfg.get("short_segment_max_words", 2)):
            return False

        combined_chars = len(previous.get("text") or "") + len(current.get("text") or "") + 1
        return combined_chars <= int(cfg.get("merge_max_chars", 220))

    @staticmethod
    def _merge_segment_into_previous(previous: dict, current: dict) -> None:
        previous["text"] = f"{str(previous.get('text') or '').rstrip()} {str(current.get('text') or '').lstrip()}".strip()
        previous["end"] = current.get("end", previous.get("end"))
        if current.get("words"):
            previous.setdefault("words", [])
            previous["words"].extend(current["words"])

    @staticmethod
    def _word_count(text: str) -> int:
        return len(re.findall(r"\w+", text, flags=re.UNICODE))

    # ── Chunking par tours pyannote ───────────────────────────────────────────

    def _build_chunks_from_turns(
        self,
        audio: np.ndarray,
        total_duration: float,
        speaker_turns: dict,
        padding_s: float = 0.15,
        max_chunk_s: int = 45,
        min_chunk_s: float = 1.5,
        chunk_cfg: dict | None = None,
    ) -> list[dict] | None:
        """Construit des chunks audio alignés sur les tours de parole pyannote.

        Utilise exclusive_speaker_diarization (pas de chevauchement) pour que chaque
        chunk corresponde à un unique locuteur. Les tours < min_chunk_s sont fusionnés
        avec le précédent s'ils partagent le même locuteur. Les tours > max_chunk_s
        sont découpés en sous-chunks de max_chunk_s avec le même speaker.

        Returns:
            Liste de chunks {start, end, speaker, audio} ou None si exclusive_turns vide.
        """
        chunk_cfg = chunk_cfg or {}
        padding_s = float(chunk_cfg.get("padding_s", padding_s))
        max_chunk_s = int(chunk_cfg.get("max_chunk_s", max_chunk_s))
        min_chunk_s = float(chunk_cfg.get("min_chunk_s", min_chunk_s))

        turns = speaker_turns.get("exclusive_turns") or []
        if not turns:
            return None
        turns = self._smooth_micro_turns(turns, chunk_cfg)

        chunks: list[dict] = []

        for turn in turns:
            start = max(0.0, turn["start"] - padding_s)
            end = min(total_duration, turn["end"] + padding_s)
            speaker = turn["speaker"]
            duration = end - start

            if duration <= 0:
                continue

            if duration <= max_chunk_s:
                if duration >= min_chunk_s:
                    chunks.append(self._make_chunk(audio, start, end, speaker))
                elif chunks and chunks[-1]["speaker"] == speaker:
                    # Tour court du même locuteur : étendre le chunk précédent
                    prev = chunks[-1]
                    prev["end"] = end
                    prev["audio"] = audio[int(prev["start"] * _SR):int(end * _SR)]
                elif duration >= 0.3:
                    # Interjection courte isolée (ok, bien, d'accord...)
                    chunks.append(self._make_chunk(audio, start, end, speaker))
            else:
                # Tour long : découper en sous-chunks, même locuteur
                pos = start
                while pos < end:
                    chunk_end = min(pos + max_chunk_s, end)
                    chunks.append(self._make_chunk(audio, pos, chunk_end, speaker))
                    pos = chunk_end

        logger.info(
            "Chunking par tours: %d exclusive_turns → %d chunks (durée moy. %.1fs)",
            len(turns),
            len(chunks),
            sum(c["end"] - c["start"] for c in chunks) / max(len(chunks), 1),
        )
        return chunks or None

    @staticmethod
    def _smooth_micro_turns(turns: list[dict], cfg: dict) -> list[dict]:
        """Fusionne uniquement les micro-tours sûrs avec un voisin du même locuteur."""
        if not cfg.get("merge_micro_chunks", True):
            return turns

        micro_s = float(cfg.get("micro_chunk_s", 0.35))
        neighbor_gap_s = float(cfg.get("micro_chunk_neighbor_gap_s", 0.4))
        smoothed: list[dict] = []

        for turn in turns:
            current = dict(turn)
            duration = float(current.get("end", 0)) - float(current.get("start", 0))
            gap = (
                float(current.get("start", 0)) - float(smoothed[-1].get("end", 0))
                if smoothed else None
            )
            if (
                duration < micro_s
                and smoothed
                and smoothed[-1].get("speaker") == current.get("speaker")
                and gap is not None
                and gap <= neighbor_gap_s
            ):
                smoothed[-1]["end"] = max(float(smoothed[-1].get("end", 0)), float(current.get("end", 0)))
                smoothed[-1]["micro_turns_merged"] = int(smoothed[-1].get("micro_turns_merged", 0)) + 1
                continue
            smoothed.append(current)

        return smoothed

    @staticmethod
    def _make_chunk(audio: np.ndarray, start: float, end: float, speaker: str) -> dict:
        return {
            "start": start,
            "end": end,
            "speaker": speaker,
            "audio": audio[int(start * _SR):int(end * _SR)],
        }

    def _apply_vad_filter(
        self, chunks: list[dict], audio: np.ndarray, sl, vad_cfg: dict | None = None
    ) -> list[dict]:
        """Filtre les chunks pyannote qui ne contiennent pas de parole selon Silero VAD.

        Exécuté une seule fois sur l'audio complet (déjà chargé) — pas de surcoût I/O.
        Si VAD indisponible ou aucune zone détectée, retourne les chunks inchangés.
        """
        from transcria.audio.vad import SileroVAD

        vad_cfg = vad_cfg or {}
        vad = SileroVAD(
            threshold=vad_cfg.get("threshold", 0.5),
            min_speech_duration_ms=vad_cfg.get("min_speech_duration_ms", 250),
            min_silence_duration_ms=vad_cfg.get("min_silence_duration_ms", 400),
            speech_pad_ms=vad_cfg.get("speech_pad_ms", 200),
            max_gap_s=vad_cfg.get("max_gap_s", 0.5),
        )
        speech_zones = vad.get_speech_timestamps(audio, _SR)
        if not speech_zones:
            sl.info("VAD: indisponible ou aucune parole détectée — pas de filtrage")
            return chunks

        filtered, removed = [], 0
        for chunk in chunks:
            overlaps = any(
                z["start"] < chunk["end"] and z["end"] > chunk["start"]
                for z in speech_zones
            )
            if overlaps:
                filtered.append(chunk)
            else:
                removed += 1
                logger.debug(
                    "VAD: chunk filtré [%.1fs-%.1fs] %s (aucune parole détectée)",
                    chunk["start"], chunk["end"], chunk["speaker"],
                )

        if removed:
            sl.info("VAD: %d chunks filtrés sur %d (bruit/silence)", removed, len(chunks))
        else:
            sl.info("VAD: tous les %d chunks contiennent de la parole", len(chunks))

        return filtered or chunks  # sécurité : ne jamais retourner une liste vide

    def _transcribe_by_chunks(
        self,
        chunks: list[dict],
        lang: str,
        speaker_mapping: dict | None,
        sl,
    ) -> list[dict]:
        """Transcrit chaque chunk et reconstruit les timestamps globaux.

        Séquentiel par défaut. Si le backend est concurrent-safe (distant) et
        `inference.stt.concurrency` > 1, les tours sont transcrits en parallèle
        (concurrence bornée) pour exploiter le batching continu de vLLM. L'ORDRE
        des segments (et donc des timestamps) est préservé dans les deux cas.
        """
        mapping = self._build_name_mapping(speaker_mapping)
        total = len(chunks)
        workers = self._chunk_concurrency(total)
        backend = getattr(self.transcriber, "model_name", self.transcriber.__class__.__name__)
        concurrent_safe = bool(getattr(self.transcriber, "concurrent_safe", False))
        started = time.monotonic()

        if workers > 1:
            sl.info(
                "Transcription par tour en concurrence: backend=%s workers=%d tours=%d",
                backend,
                workers,
                total,
            )
            concurrent_segments = self._transcribe_chunks_concurrent(chunks, lang, mapping, workers, sl)
            self._last_chunk_metrics = self._log_chunk_transcription_summary(
                sl,
                backend,
                total,
                len(concurrent_segments),
                started,
                workers=workers,
                concurrent_safe=concurrent_safe,
            )
            return concurrent_segments

        transcribe_prechunked = getattr(self.transcriber, "transcribe_prechunked", None)
        if callable(transcribe_prechunked):
            sl.info("Transcription par tours batchée: backend=%s tours=%d", backend, total)
            batched_segments = transcribe_prechunked(
                chunks,
                language=lang,
                speaker_mapping=mapping,
            )
            batched_segments = [seg for seg in batched_segments if not seg.get("error")]
            self._last_chunk_metrics = self._log_chunk_transcription_summary(
                sl,
                backend,
                total,
                len(batched_segments),
                started,
                workers=1,
                concurrent_safe=concurrent_safe,
            )
            return batched_segments

        sl.info(
            "Transcription par tour séquentielle: backend=%s tours=%d concurrent_safe=%s",
            backend,
            total,
            concurrent_safe,
        )
        segments: list[dict] = []
        for i, chunk in enumerate(chunks):
            segments.extend(self._process_chunk(chunk, lang, mapping))
            if (i + 1) % 100 == 0 or (i + 1) == total:
                sl.info("Progression transcription: %d/%d chunks", i + 1, total)
        self._last_chunk_metrics = self._log_chunk_transcription_summary(
            sl,
            backend,
            total,
            len(segments),
            started,
            workers=1,
            concurrent_safe=concurrent_safe,
        )
        return segments

    def _process_chunk(self, chunk: dict, lang: str, mapping: dict) -> list[dict]:
        """Transcrit un tour et recale ses timestamps/locuteur (pur, thread-safe)."""
        out: list[dict] = []
        chunk_segments = self.transcriber.transcribe(
            audio_path=None,
            language=lang,
            audio_array=chunk["audio"],
            sample_rate=_SR,
        )
        for seg in chunk_segments:
            if seg.get("error"):
                continue
            seg["start"] = round(chunk["start"] + seg["start"], 3)
            seg["end"] = round(chunk["start"] + seg["end"], 3)
            self._offset_words(seg, chunk["start"])
            raw_speaker = chunk["speaker"]
            seg["speaker"] = mapping.get(raw_speaker, raw_speaker)
            out.append(seg)
        return out

    def _chunk_concurrency(self, total: int) -> int:
        """Nombre de tours transcrits en parallèle (1 = séquentiel).

        > 1 seulement si le backend est concurrent-safe (distant) ET
        `inference.stt.concurrency` > 1. Borné par le nombre de tours.
        """
        if total <= 1 or not getattr(self.transcriber, "concurrent_safe", False):
            return 1
        stt_cfg = (self.config.get("inference", {}) or {}).get("stt", {}) or {}
        raw_workers = stt_cfg.get("concurrency", 1)
        try:
            workers = int(raw_workers)
        except (TypeError, ValueError):
            logger.warning("inference.stt.concurrency invalide (%r) — retour au mode séquentiel", raw_workers)
            return 1
        if workers < 1:
            logger.warning("inference.stt.concurrency doit être >= 1 (%r) — retour au mode séquentiel", raw_workers)
            return 1
        return max(1, min(workers, total))

    def _transcribe_chunks_concurrent(
        self, chunks: list[dict], lang: str, mapping: dict, workers: int, sl,
    ) -> list[dict]:
        """Transcrit les tours en parallèle ; `executor.map` préserve l'ordre d'entrée."""
        from concurrent.futures import ThreadPoolExecutor

        segments: list[dict] = []
        total = len(chunks)
        done = 0
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="stt-chunk") as ex:
            for chunk_segments in ex.map(lambda c: self._process_chunk(c, lang, mapping), chunks):
                segments.extend(chunk_segments)
                done += 1
                if done % 100 == 0 or done == total:
                    sl.info("Progression transcription: %d/%d tours", done, total)
        return segments

    @staticmethod
    def _log_chunk_transcription_summary(
        sl,
        backend: str,
        total_chunks: int,
        segment_count: int,
        started: float,
        *,
        workers: int,
        concurrent_safe: bool,
    ) -> dict:
        elapsed = max(0.001, time.monotonic() - started)
        chunks_per_s = total_chunks / elapsed
        segments_per_s = segment_count / elapsed
        sl.info(
            "Transcription par tour terminée: backend=%s workers=%d tours=%d segments=%d duree=%.2fs tours_s=%.2f segments_s=%.2f",
            backend,
            workers,
            total_chunks,
            segment_count,
            elapsed,
            chunks_per_s,
            segments_per_s,
        )
        return {
            "mode": "concurrent" if workers > 1 else "sequential",
            "backend": backend,
            "workers": workers,
            "concurrent_safe": concurrent_safe,
            "chunks": total_chunks,
            "segments": segment_count,
            "elapsed_s": round(elapsed, 3),
            "chunks_per_s": round(chunks_per_s, 3),
            "segments_per_s": round(segments_per_s, 3),
        }

    @staticmethod
    def _build_name_mapping(speaker_mapping: dict | None) -> dict:
        """Construit le dict {speaker_id: nom_affiché} depuis speaker_mapping.json."""
        if not speaker_mapping:
            return {}
        mapping = dict(speaker_mapping.get("mapping", {}))
        for s in speaker_mapping.get("speakers", []):
            if s.get("mapped_name"):
                mapping[s["speaker_id"]] = s["mapped_name"]
        return mapping

    @staticmethod
    def _offset_words(seg: dict, offset_s: float) -> None:
        for word in seg.get("words") or []:
            if "start" in word:
                word["start"] = round(offset_s + word["start"], 3)
            if "end" in word:
                word["end"] = round(offset_s + word["end"], 3)

    def _apply_forced_alignment_if_enabled(
        self,
        audio_path: Path,
        segments: list[dict],
        language: str,
        backend: str,
        sl,
    ) -> list[dict]:
        if backend != "whisper":
            return segments
        try:
            from transcria.stt.forced_alignment import ForcedAlignmentService

            device = f"cuda:{self.gpu_index}" if self.gpu_index is not None else "cpu"
            aligner = ForcedAlignmentService(self.config, device=device)
            aligned = aligner.align_segments(audio_path, segments, language=language)
            if aligned is not segments:
                sl.info("Alignement mot-à-mot terminé", backend=backend)
            return aligned
        except Exception as exc:
            logger.warning("Alignement mot-à-mot ignoré: %s", exc)
            return segments

    def _apply_speaker_realignment(
        self,
        segments: list[dict],
        speaker_turns: dict | None,
        speaker_mapping: dict | None,
        sl,
    ) -> list[dict]:
        try:
            from transcria.stt.speaker_realignment import SpeakerPunctuationRealigner

            realigned = SpeakerPunctuationRealigner(self.config).realign(
                segments, speaker_turns, speaker_mapping
            )
            if realigned is not segments:
                sl.info("Réalignement locuteur mot-à-mot appliqué", segments=len(realigned))
            return realigned
        except Exception as exc:
            logger.warning("Réalignement locuteur mot-à-mot ignoré: %s", exc)
            return segments

    def _score_segment_reliability(self, segments: list[dict], fs: JobFilesystem, sl) -> list[dict]:
        """Ajoute un score de fiabilité par segment sans modifier le texte."""
        try:
            from transcria.stt.reliability import SegmentReliabilityScorer

            preflight = fs.load_json("metadata/audio_preflight.json") or {}
            scored = SegmentReliabilityScorer(self.config).score_segments(segments, preflight)
            counts: dict[str, int] = {}
            for segment in scored:
                level = str(segment.get("reliability") or "unknown")
                counts[level] = counts.get(level, 0) + 1
            if counts:
                sl.info("Fiabilité segments ASR calculée", reliability_counts=counts)
            return scored
        except Exception as exc:
            logger.warning("Scorage fiabilité segments ignoré: %s", exc)
            return segments

    def _write_stt_corpus(self, job: Job, segments: list[dict], backend: str, fs: JobFilesystem, sl) -> dict | None:
        """Écrit le corpus difficulté↔qualité par segment (brique 2 de calibration).

        Persiste `metadata/stt_corpus.json` (lignes par segment) et promeut un
        agrégat compact dans `extra_data.stt_corpus_summary` (requêtable cross-jobs).
        Best-effort : aucune erreur ne doit interrompre la transcription.
        """
        corpus_cfg = self.config.get("workflow", {}).get("stt_corpus", {}) or {}
        if not corpus_cfg.get("enabled", True):
            return None
        try:
            from transcria.stt.corpus import build_segment_corpus, summarize_corpus

            preflight = fs.load_json("metadata/audio_preflight.json") or {}
            corpus = build_segment_corpus(segments, backend, preflight.get("difficulty_map") or [])
            fs.save_json("metadata/stt_corpus.json", corpus)
            summary = summarize_corpus(corpus)
            sl.info(
                "Corpus STT par segment écrit",
                segments=summary.get("segments"),
                by_difficulty={k: v.get("count") for k, v in (summary.get("by_difficulty") or {}).items()},
            )
            try:
                from transcria.jobs.store import JobStore

                JobStore.update_extra_data(job.id, lambda extra: {**extra, "stt_corpus_summary": summary})
            except Exception as exc:
                logger.warning("Promotion stt_corpus_summary en base ignorée: %s", exc)
            return summary
        except Exception as exc:
            logger.warning("Écriture corpus STT ignorée: %s", exc)
            return None

    def _backend_metadata(self) -> dict:
        getter = getattr(self.transcriber, "get_metadata", None)
        if not callable(getter):
            return {}
        try:
            metadata = getter()
        except Exception as exc:
            logger.warning("Métadonnées backend STT indisponibles: %s", exc)
            return {}
        return metadata if isinstance(metadata, dict) else {}

    # ── Fallback overlap matching (conservé pour le mode 30s) ─────────────────

    def _apply_speakers(
        self, segments: list[dict], speaker_turns: dict, speaker_mapping: dict | None = None
    ) -> list[dict]:
        """Assigne le locuteur majoritaire à chaque segment (fallback chunking 30s)."""
        turns = speaker_turns.get("turns", [])
        if not turns:
            return segments

        mapping = self._build_name_mapping(speaker_mapping)

        for seg in segments:
            best_speaker = None
            best_overlap = 0.0
            seg_start = seg.get("start", 0)
            seg_end = seg.get("end", 0)

            for turn in turns:
                t_start = turn.get("start", 0)
                t_end = turn.get("end", 0)
                overlap = max(0, min(seg_end, t_end) - max(seg_start, t_start))
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_speaker = turn.get("speaker")

            if best_speaker:
                seg["speaker"] = mapping.get(best_speaker, best_speaker)

        return segments

import logging
import re
from pathlib import Path

import numpy as np

from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job
from transcria.jobs.store import JobStore
from transcria.stt.transcriber_factory import create_transcriber
from transcria.logging_setup import get_structured_logger

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


class Transcriber:
    def __init__(self, config: dict, gpu_index: int = 0):
        self.config = config
        device = f"cuda:{gpu_index}" if gpu_index is not None else "cuda:0"
        self.transcriber = create_transcriber(config, device=device)
        self.gpu_index = gpu_index

    # ── API publique ──────────────────────────────────────────────────────────

    def transcribe(self, job: Job, audio_path: Path) -> dict:
        import librosa

        fs = JobFilesystem(self.config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
        sl = get_structured_logger(__name__)
        sl.set_context(job_id=job.id, step="transcribe")

        lang = job.get_extra_data().get("meeting_context", {}).get("language", "fr")
        backend = self.config.get("models", {}).get("stt_backend", "cohere")

        speaker_turns = fs.load_json("speakers/speaker_turns.json")
        speaker_mapping = fs.load_json("speakers/speaker_mapping.json")

        sl.info("DÉBUT transcription", backend=backend, gpu=self.gpu_index)

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

        # Choisir le mode de chunking selon la disponibilité des exclusive_turns
        if speaker_turns and speaker_turns.get("exclusive_turns"):
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
            sl.info("Mode transcription: 30s fixes (exclusive_turns absent)", backend=backend)
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
        segments = self._cleanup_transcription_segments(segments, sl)
        segments = self._score_segment_reliability(segments, fs, sl)
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
            "gpu_index": self.gpu_index,
            "language": lang,
            "segments": len(segments),
            "speaker_count": speaker_count,
            "vad_final_enabled": vad_enabled,
            "backend_metadata_path": f"metadata/{backend}.json" if backend_metadata else None,
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

    def _cleanup_transcription_segments(self, segments: list[dict], sl=None) -> list[dict]:
        """Nettoie les artefacts ASR mesurés et fusionne les micro-segments sûrs."""
        cfg = self.config.get("workflow", {}).get("transcription_cleanup", {}) or {}
        if not cfg.get("enabled", True):
            return segments

        remove_artifacts = cfg.get("remove_subtitle_artifacts", True)
        merge_short = cfg.get("merge_short_segments", True)

        artifact_patterns = self._build_artifact_patterns(cfg)
        artifact_words = self._build_artifact_words(cfg)

        cleaned: list[dict] = []
        removed_artifacts = 0
        merged_short = 0

        for segment in segments:
            text = str(segment.get("text") or "").strip()
            if not text:
                continue
            if remove_artifacts and self._is_subtitle_artifact(text, artifact_patterns, artifact_words):
                removed_artifacts += 1
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

        if sl and (removed_artifacts or merged_short):
            sl.info(
                "Nettoyage transcription appliqué",
                removed_artifacts=removed_artifacts,
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
        max_chunk_s: int = 30,
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
        """Transcrit chaque chunk et reconstruit les timestamps globaux."""
        mapping = self._build_name_mapping(speaker_mapping)
        segments: list[dict] = []
        total = len(chunks)

        for i, chunk in enumerate(chunks):
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
                segments.append(seg)

            if (i + 1) % 100 == 0 or (i + 1) == total:
                sl.info("Progression transcription: %d/%d chunks", i + 1, total)

        return segments

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

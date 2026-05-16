import logging
from pathlib import Path

import numpy as np

from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job
from transcria.jobs.store import JobStore
from transcria.stt.transcriber_factory import create_transcriber
from transcria.logging_setup import get_structured_logger

logger = logging.getLogger(__name__)

_SR = 16000


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

        # Choisir le mode de chunking selon la disponibilité des exclusive_turns
        if speaker_turns and speaker_turns.get("exclusive_turns"):
            # Chunking par tours pyannote : charger l'audio une seule fois en mémoire,
            # passer des numpy arrays à chaque chunk → pas de fichiers WAV temporaires.
            audio, sr = librosa.load(str(audio_path), sr=_SR, mono=True)
            total_duration = len(audio) / sr
            chunks = self._build_chunks_from_turns(audio, total_duration, speaker_turns)
            if chunks:
                sl.info("Mode transcription: tours pyannote (%d chunks)", len(chunks), backend=backend)
                segments = self._transcribe_by_chunks(chunks, lang, speaker_mapping, sl)
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

        speaker_map = speaker_mapping or {}
        srt_content = self.transcriber.segments_to_srt(segments, speaker_map.get("mapping"))
        fs.save_text("metadata/transcription.srt", srt_content)
        fs.save_json("metadata/transcription_segments.json", segments)
        fs.save_json("metadata/speakers_map.json", speaker_map)

        speaker_count = len(set(s.get("speaker", "") for s in segments if s.get("speaker")))
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

    # ── Chunking par tours pyannote ───────────────────────────────────────────

    def _build_chunks_from_turns(
        self,
        audio: np.ndarray,
        total_duration: float,
        speaker_turns: dict,
        padding_s: float = 0.15,
        max_chunk_s: int = 30,
        min_chunk_s: float = 1.5,
    ) -> list[dict] | None:
        """Construit des chunks audio alignés sur les tours de parole pyannote.

        Utilise exclusive_speaker_diarization (pas de chevauchement) pour que chaque
        chunk corresponde à un unique locuteur. Les tours < min_chunk_s sont fusionnés
        avec le précédent s'ils partagent le même locuteur. Les tours > max_chunk_s
        sont découpés en sous-chunks de max_chunk_s avec le même speaker.

        Returns:
            Liste de chunks {start, end, speaker, audio} ou None si exclusive_turns vide.
        """
        turns = speaker_turns.get("exclusive_turns") or []
        if not turns:
            return None

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
    def _make_chunk(audio: np.ndarray, start: float, end: float, speaker: str) -> dict:
        return {
            "start": start,
            "end": end,
            "speaker": speaker,
            "audio": audio[int(start * _SR):int(end * _SR)],
        }

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

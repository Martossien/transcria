import logging
from pathlib import Path

from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job, JobState
from transcria.jobs.store import JobStore
from transcria.stt.cohere_transcriber import CohereTranscriber

logger = logging.getLogger(__name__)


class Transcriber:
    def __init__(self, config: dict, gpu_index: int = 0):
        self.config = config
        model_path = config.get("models", {}).get("cohere_model_path")
        device = f"cuda:{gpu_index}" if gpu_index is not None else "cuda:0"
        self.cohere = CohereTranscriber(model_path=model_path, device=device)
        self.gpu_index = gpu_index

    def transcribe(self, job: Job, audio_path: Path) -> dict:
        fs = JobFilesystem(self.config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)

        lang = job.get_extra_data().get("meeting_context", {}).get("language", "fr")

        segments = self.cohere.transcribe(audio_path, language=lang)

        # Toujours appliquer la diarisation si les turns existent
        speaker_turns = fs.load_json("speakers/speaker_turns.json")
        speaker_mapping = fs.load_json("speakers/speaker_mapping.json")
        if speaker_turns and speaker_turns.get("turns"):
            segments = self._apply_speakers(segments, speaker_turns, speaker_mapping)

        speaker_map = speaker_mapping or {}
        srt_content = self.cohere.segments_to_srt(segments, speaker_map.get("mapping"))
        fs.save_text("metadata/transcription.srt", srt_content)
        fs.save_json("metadata/transcription_segments.json", segments)
        fs.save_json("metadata/speakers_map.json", speaker_map)

        return {
            "segments": segments,
            "srt_content": srt_content,
            "speaker_count": len(set(s.get("speaker", "") for s in segments if s.get("speaker"))),
        }

    def _apply_speakers(self, segments: list[dict], speaker_turns: dict, speaker_mapping: dict = None) -> list[dict]:
        turns = speaker_turns.get("turns", [])
        if not turns:
            return segments

        mapping = {}
        if speaker_mapping:
            mapping = speaker_mapping.get("mapping", {})
            # aussi chercher dans speakers
            for s in speaker_mapping.get("speakers", []):
                if s.get("mapped_name"):
                    mapping[s["speaker_id"]] = s["mapped_name"]

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
                mapped = mapping.get(best_speaker, best_speaker)
                seg["speaker"] = mapped

        return segments

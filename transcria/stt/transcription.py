import logging
from pathlib import Path

from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job, JobState
from transcria.jobs.store import JobStore
from transcria.stt.transcriber_factory import create_transcriber
from transcria.logging_setup import get_structured_logger

logger = logging.getLogger(__name__)


class Transcriber:
    def __init__(self, config: dict, gpu_index: int = 0):
        self.config = config
        device = f"cuda:{gpu_index}" if gpu_index is not None else "cuda:0"
        self.transcriber = create_transcriber(config, device=device)
        self.gpu_index = gpu_index

    def transcribe(self, job: Job, audio_path: Path) -> dict:
        fs = JobFilesystem(self.config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
        sl = get_structured_logger(__name__)
        sl.set_context(job_id=job.id, step="transcribe")

        lang = job.get_extra_data().get("meeting_context", {}).get("language", "fr")
        backend = self.config.get("models", {}).get("stt_backend", "cohere")

        sl.info("DÉBUT transcription", backend=backend, gpu=self.gpu_index)
        segments = self.transcriber.transcribe(audio_path, language=lang)

        speaker_turns = fs.load_json("speakers/speaker_turns.json")
        speaker_mapping = fs.load_json("speakers/speaker_mapping.json")
        if speaker_turns and speaker_turns.get("turns"):
            segments = self._apply_speakers(segments, speaker_turns, speaker_mapping)

        speaker_map = speaker_mapping or {}
        srt_content = self.transcriber.segments_to_srt(segments, speaker_map.get("mapping"))
        fs.save_text("metadata/transcription.srt", srt_content)
        fs.save_json("metadata/transcription_segments.json", segments)
        fs.save_json("metadata/speakers_map.json", speaker_map)

        speaker_count = len(set(s.get("speaker", "") for s in segments if s.get("speaker")))
        sl.info("FIN transcription", segments=len(segments), speakers=speaker_count,
                srt_chars=len(srt_content), backend=backend)

        return {
            "segments": segments,
            "srt_content": srt_content,
            "speaker_count": speaker_count,
        }

    def _apply_speakers(self, segments: list[dict], speaker_turns: dict, speaker_mapping: dict = None) -> list[dict]:
        turns = speaker_turns.get("turns", [])
        if not turns:
            return segments

        mapping = {}
        if speaker_mapping:
            mapping = speaker_mapping.get("mapping", {})
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

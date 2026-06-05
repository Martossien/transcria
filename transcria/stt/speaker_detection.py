import logging
import re
from pathlib import Path

from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job

logger = logging.getLogger(__name__)


class SpeakerDetector:
    def __init__(self, config: dict):
        self.config = config

    def detect(self, job: Job, audio_path: Path, device: str = "cuda:0", progress_callback=None) -> dict:
        fs = JobFilesystem(self.config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)

        diar_result = fs.load_json("speakers/speaker_turns.json")
        if diar_result is None:
            from transcria.stt.diarizer_factory import create_diarizer

            ds = create_diarizer(self.config, device=device, progress_callback=progress_callback)
            diar_result = ds.diarize(job, audio_path)
        elif diar_result.get("available") and fs.load_json("speakers/speaker_clips.json") is None:
            from transcria.stt.diarizer_factory import create_diarizer

            ds = create_diarizer(self.config, device=device)
            ds._extract_clips(
                audio_path,
                diar_result.get("turns", []),
                diar_result.get("speakers", []),
                fs,
            )

        if not diar_result.get("available"):
            return {
                "available": False,
                "message": diar_result.get("message", "Détection locuteurs indisponible."),
                "speakers": [],
            }

        speakers = []
        for spk in diar_result.get("speakers", []):
            stats = diar_result.get("stats", {}).get(spk, {})
            speakers.append({
                "speaker_id": spk,
                "label": spk,
                "speaking_time_seconds": stats.get("speaking_time_seconds", 0),
                "turn_count": stats.get("turn_count", 0),
                "mapped_to": None,
                "mapped_name": None,
                "validation": "pending",
            })

        fs.save_json("speakers/speaker_stats.json", {"speakers": speakers})
        return {"available": True, "speakers": speakers, "turns": diar_result.get("turns", [])}

    @staticmethod
    def _clean_name(raw_name: str, speaker_id: str) -> str:
        cleaned = re.sub(r"\s*\(SPEAKER_\d+[^)]*\)\s*", "", raw_name).strip()
        cleaned = re.sub(r"\s*\(\s*\d+\s*tours?\s*[^)]*\)\s*", "", cleaned).strip()
        cleaned = re.sub(r"\s*\(\s*~\d+\s*min[^)]*\)\s*", "", cleaned).strip()
        return cleaned if cleaned else speaker_id

    @staticmethod
    def save_mapping(job_id: str, jobs_dir: str, mapping: dict) -> bool:
        fs = JobFilesystem(jobs_dir, job_id)
        speakers_data = fs.load_json("speakers/speaker_stats.json") or {}
        speakers = speakers_data.get("speakers", [])

        for spk in speakers:
            spk_id = spk.get("speaker_id")
            if spk_id in mapping:
                spk["mapped_to"] = mapping[spk_id].get("participant_id")
                raw_name = mapping[spk_id].get("name", spk_id)
                spk["mapped_name"] = SpeakerDetector._clean_name(raw_name, spk_id)
                spk["gender"] = mapping[spk_id].get("gender", "")
                spk["validation"] = "user_validated"

        fs.save_json("speakers/speaker_stats.json", {"speakers": speakers})
        fs.save_json("speakers/speaker_mapping.json", {"mapping": mapping, "speakers": speakers})
        participant_list = mapping.get("__participants__", [])
        if participant_list:
            fs.save_json("context/participants.json", participant_list)

        return True

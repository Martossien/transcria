import logging
import re
from pathlib import Path

from transcria.ingestion.manifest import parse_participants_manifest
from transcria.ingestion.manifest_turns import turns_from_manifest
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job
from transcria.stt.diarizer_factory import create_diarizer

logger = logging.getLogger(__name__)


class SpeakerDetector:
    def __init__(self, config: dict):
        self.config = config

    def detect(self, job: Job, audio_path: Path, device: str = "cuda:0", progress_callback=None) -> dict:
        fs = JobFilesystem(self.config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)

        diar_result = fs.load_json("speakers/speaker_turns.json")
        if diar_result is None:
            # RÉUNION avec manifeste (décision utilisateur 2026-07-29) : les tours viennent
            # des PISTES — segmentation exacte, parole simultanée comprise, jamais de
            # sur-découpage d'une voix unique. pyannote ne sert que sans manifeste.
            # Compromis assumé : une piste « salle » = UN locuteur nommable (vague 5 : pistes
            # séparées pour scinder une salle).
            diar_result = self._turns_from_meeting_manifest(fs)
        if diar_result is None:
            ds = create_diarizer(self.config, device=device, progress_callback=progress_callback)
            diar_result = ds.diarize(job, audio_path)
        elif diar_result.get("available") and fs.load_json("speakers/speaker_clips.json") is None:
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
        manifest_names = diar_result.get("names", {}) if diar_result.get("source") == "manifest" else {}
        for spk in diar_result.get("speakers", []):
            stats = diar_result.get("stats", {}).get(spk, {})
            entry = {
                "speaker_id": spk,
                "label": spk,
                "speaking_time_seconds": stats.get("speaking_time_seconds", 0),
                "turn_count": stats.get("turn_count", 0),
                "mapped_to": None,
                "mapped_name": None,
                "validation": "pending",
            }
            if manifest_names.get(spk):
                # Nom connu de la plateforme → PRÉ-REMPLI à l'étape 5 (suggestion, jamais
                # une validation : l'humain reste le juge).
                entry["suggested_name"] = manifest_names[spk]
                entry["suggested_source"] = "meeting"
            speakers.append(entry)

        fs.save_json("speakers/speaker_stats.json", {"speakers": speakers})
        return {"available": True, "speakers": speakers, "turns": diar_result.get("turns", [])}


    def _turns_from_meeting_manifest(self, fs) -> dict | None:
        """Tours de parole depuis `metadata/participants_manifest.json` s'il existe et
        valide — sinon None (le diarizeur classique prend la main)."""
        raw = fs.load_json("metadata/participants_manifest.json")
        if not raw:
            return None
        manifest, _reason = parse_participants_manifest(raw)
        if manifest is None:
            return None
        result = turns_from_manifest(manifest)
        if not result.get("available"):
            return None
        fs.save_json("speakers/speaker_turns.json", result)
        return result

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

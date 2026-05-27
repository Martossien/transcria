import logging
from pathlib import Path

from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import JobState
from transcria.jobs.store import JobStore
from transcria.logging_setup import get_structured_logger

logger = logging.getLogger(__name__)


class JobService:

    @staticmethod
    def create(owner_id: str, title: str) -> dict:
        job = JobStore.create_job(owner_id, title)
        sl = get_structured_logger(__name__)
        sl.info("Job créé", job_id=job.id, owner_id=owner_id)
        return {"job_id": job.id, "title": job.title, "state": job.state}

    @staticmethod
    def upload(job_id: str, file_data: bytes, filename: str, jobs_dir: str) -> dict:

        job = JobStore.get_by_id(job_id)
        if job is None:
            return {"error": "Job introuvable"}

        fs = JobFilesystem(jobs_dir, job.id)
        result = fs.save_upload(file_data, filename)

        stem = Path(filename).stem or filename
        current_title = (job.title or "").strip()
        if not current_title or current_title == "Réunion sans titre":
            job.title = stem[:255]

        JobStore.update_state(job.id, JobState.UPLOADED)

        sl = get_structured_logger(__name__)
        sl.info("Fichier uploadé", job_id=job.id, filename=filename,
                size_bytes=result.get("size_bytes", 0))

        return result

    @staticmethod
    def analyze(job_id: str, jobs_dir: str, config: dict) -> dict:
        from transcria.audio.analyzer import AudioAnalyzer
        from transcria.audio.preflight import AudioPreflightAnalyzer
        from transcria.quality.audio_quality import AudioQualityEvaluator

        job = JobStore.get_by_id(job_id)
        if job is None:
            return {"error": "Job introuvable"}

        fs = JobFilesystem(jobs_dir, job.id)
        audio_path = fs.get_original_audio_path()
        if audio_path is None:
            return {"error": "Aucun fichier audio"}

        result: dict = AudioAnalyzer.analyze(audio_path)
        fs.save_json("metadata/audio_analysis.json", result)
        preflight = {}
        try:
            analyzer = AudioPreflightAnalyzer(config)
            if analyzer.enabled:
                preflight = analyzer.analyze(audio_path)
                if preflight:
                    fs.save_json("metadata/audio_preflight.json", preflight)
        except Exception as exc:
            logger.warning("Pré-diagnostic audio indisponible pour %s: %s", job.id, exc)
        try:
            quality_decision = AudioQualityEvaluator(config).evaluate(
                result,
                _quality_summary_from_preflight(preflight),
            )
            fs.save_json("metadata/audio_quality_decision.json", quality_decision)
        except Exception as exc:
            logger.warning("Décision qualité audio indisponible pour %s: %s", job.id, exc)
        JobStore.update_state(job.id, JobState.ANALYZED)

        sl = get_structured_logger(__name__)
        sl.info("Audio analysé", job_id=job.id,
                duree=result.get("duration_seconds", 0),
                codec=result.get("codec", "?"),
                audio_risk=preflight.get("risk_level"),
                audio_flags=preflight.get("flags"))

        return result

    @staticmethod
    def get_context(job_id: str, jobs_dir: str) -> dict:
        fs = JobFilesystem(jobs_dir, job_id)
        job = JobStore.get_by_id(job_id)

        summary = fs.load_text("summary/summary.md") or ""
        meeting_ctx = fs.load_json("context/meeting_context.json") or {}
        lexicon = fs.load_json("context/session_lexicon.json") or []
        speaker_stats = fs.load_json("speakers/speaker_stats.json") or {}
        speaker_mapping = fs.load_json("speakers/speaker_mapping.json") or {}
        participants = fs.load_json("context/participants.json") or []
        analysis = fs.load_json("metadata/audio_analysis.json") or {}
        quality_report = fs.load_json("quality/quality_report.json") or {}

        merged = _merge_speakers_with_participants(speaker_mapping, participants)

        return {
            "job": job,
            "summary": summary,
            "meeting_context": meeting_ctx,
            "lexicon": lexicon,
            "speakers": speaker_stats.get("speakers", []),
            "speaker_count": speaker_stats.get("speaker_count", 0),
            "speaker_mapping": merged,
            "participants": participants,
            "analysis": analysis,
            "quality_report": quality_report,
        }

    @staticmethod
    def delete(job_id: str, jobs_dir: str) -> bool:
        job = JobStore.get_by_id(job_id)
        if job is None:
            return False
        fs = JobFilesystem(jobs_dir, job.id)
        fs.cleanup()
        JobStore.delete_job(job.id)
        logger.info("Job supprimé: %s", job.id)
        return True


def _merge_speakers_with_participants(
    mapping: dict, participants: list[dict]
) -> list[dict]:
    result = []
    for spk_id, info in (mapping or {}).items():
        entry: dict = {
            "speaker_id": spk_id,
            "name": info if isinstance(info, str) else (info.get("name", spk_id) if isinstance(info, dict) else spk_id),
            "participant_id": info.get("participant_id", "") if isinstance(info, dict) else "",
        }
        if isinstance(info, dict) and info.get("participant_id"):
            for p in participants:
                if p.get("id") == info["participant_id"]:
                    entry["participant"] = p
                    break
        result.append(entry)
    return result


def _quality_summary_from_preflight(preflight: dict) -> dict:
    """Expose le pré-diagnostic sous le format attendu par AudioQualityEvaluator."""
    if not preflight:
        return {}
    level = str(preflight.get("risk_level") or "").strip()
    if not level:
        return {}
    return {"diagnostics": {"level": level}}

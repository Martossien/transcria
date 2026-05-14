import yaml
from datetime import datetime, timezone

from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job


class JobContextBuilder:
    @staticmethod
    def build(job: Job, jobs_dir: str) -> dict:
        fs = JobFilesystem(jobs_dir, job.id)

        meeting = fs.load_json("context/meeting_context.json") or {}
        participants = fs.load_json("context/participants.json") or []
        speakers_data = fs.load_json("speakers/speaker_mapping.json") or {}
        lexicon = fs.load_json("context/session_lexicon.json") or []

        context = {
            "job_id": job.id,
            "owner_user_id": job.owner_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "meeting": {
                "title": meeting.get("title", ""),
                "type": meeting.get("meeting_type", ""),
                "date": meeting.get("date", ""),
                "language": meeting.get("language", "fr"),
                "summary_control": meeting.get("summary", ""),
                "notes": meeting.get("notes", ""),
            },
            "participants": [
                {
                    "id": p.get("id", ""),
                    "name": p.get("name", ""),
                    "function": p.get("function", ""),
                    "role": p.get("role", ""),
                    "expected": p.get("expected", True),
                }
                for p in participants
            ],
            "speakers": [
                {
                    "speaker_id": s.get("speaker_id", ""),
                    "mapped_to": s.get("mapped_to"),
                    "mapped_name": s.get("mapped_name", ""),
                    "speaking_time_seconds": s.get("speaking_time_seconds", 0),
                    "validation": s.get("validation", "pending"),
                }
                for s in speakers_data.get("speakers", [])
            ],
            "lexicon": [
                {
                    "term": t.get("term", ""),
                    "category": t.get("category", ""),
                    "priority": t.get("priority", "normale"),
                    "variants": t.get("variants", []),
                }
                for t in lexicon
            ],
            "processing": {
                "default_stt_model": "cohere-transcribe-03-2026",
                "diarization_model": "pyannote/speaker-diarization-community-1",
            },
        }

        fs.save_text("context/job_context.yaml", yaml.dump(context, allow_unicode=True, default_flow_style=False))
        fs.save_text("context/job_context.json", __import__("json").dumps(context, ensure_ascii=False, indent=2, default=str))
        return context

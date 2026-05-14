from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job


class ParticipantsManager:
    @staticmethod
    def get(job: Job, jobs_dir: str) -> list[dict]:
        fs = JobFilesystem(jobs_dir, job.id)
        data = fs.load_json("context/participants.json")
        return data if isinstance(data, list) else []

    @staticmethod
    def save(job: Job, jobs_dir: str, participants: list[dict]) -> list[dict]:
        fs = JobFilesystem(jobs_dir, job.id)
        validated = []
        for i, p in enumerate(participants):
            entry = {
                "id": p.get("id", f"p{i + 1}"),
                "name": p.get("name", "").strip(),
                "function": p.get("function", "").strip(),
                "service": p.get("service", "").strip(),
                "role": p.get("role", "").strip(),
                "is_animator": p.get("is_animator", False),
                "expected": p.get("expected", True),
                "comment": p.get("comment", "").strip(),
            }
            validated.append(entry)
        fs.save_json("context/participants.json", validated)
        return validated

    @staticmethod
    def default_participant() -> dict:
        return {
            "id": "",
            "name": "",
            "function": "",
            "service": "",
            "role": "",
            "is_animator": False,
            "expected": True,
            "comment": "",
        }

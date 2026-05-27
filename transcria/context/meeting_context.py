from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job

MEETING_TYPES = [
    "Réunion interne",
    "Réunion projet",
    "Réunion technique",
    "Formation",
    "Réunion médicale / santé",
    "RH",
    "Entretien",
    "Autre",
]


class MeetingContextManager:
    @staticmethod
    def get(job: Job, jobs_dir: str) -> dict:
        fs = JobFilesystem(jobs_dir, job.id)
        data = fs.load_json("context/meeting_context.json")
        if data is None:
            return MeetingContextManager.default_context()
        return data

    @staticmethod
    def save(job: Job, jobs_dir: str, context_data: dict) -> dict:
        fs = JobFilesystem(jobs_dir, job.id)
        existing = fs.load_json("context/meeting_context.json") or {}
        # Préserver les champs LLM qui ne viennent pas du formulaire
        llm_fields = ["summary_llm", "title_suggere", "type_suggere", "sujet_suggere",
                       "objectif_suggere", "notes_suggeres", "participants_detectes",
                       "speaker_count_llm", "speaker_count_pyannote", "mots_cles",
                       "speaker_roles_llm", "termes_suspects",
                       "termes_suspects_parse_status", "termes_suspects_parse_warning"]
        for field in llm_fields:
            if field in existing and field not in context_data:
                context_data[field] = existing[field]
        merged = MeetingContextManager.default_context()
        merged.update(existing)
        merged.update(context_data)
        fs.save_json("context/meeting_context.json", merged)
        return merged

    @staticmethod
    def auto_suggest(job: Job, jobs_dir: str) -> dict:
        fs = JobFilesystem(jobs_dir, job.id)
        summary_text = ""
        md_text = fs.load_text("summary/summary.md")
        if md_text:
            summary_text = md_text

        suggestions = {
            "title_suggere": f"Réunion - {job.title}",
            "type_suggere": "Réunion interne",
            "sujet_suggere": "",
            "mots_cles_suggeres": [],
        }

        if summary_text:
            lines = summary_text.split("\n")
            if lines:
                suggestions["title_suggere"] = lines[0].replace("#", "").strip()[:120]

        return suggestions

    @staticmethod
    def default_context() -> dict:
        return {
            "title": "",
            "date": "",
            "meeting_type": "Réunion interne",
            "language": "fr",
            "service": "",
            "topic": "",
            "objective": "",
            "notes": "",
            "sensitivity": "normal",
        }

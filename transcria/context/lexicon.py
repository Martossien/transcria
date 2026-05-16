from pathlib import Path

from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job


LEXICON_CATEGORIES = [
    "personne", "application", "sigle", "projet", "service",
    "métier", "médical", "technique", "lieu", "autre",
]

LEXICON_PRIORITIES = ["critique", "importante", "normale"]


class LexiconManager:
    @staticmethod
    def get(job: Job, jobs_dir: str) -> list[dict]:
        fs = JobFilesystem(jobs_dir, job.id)
        data = fs.load_json("context/session_lexicon.json")
        return data if isinstance(data, list) else []

    @staticmethod
    def save(job: Job, jobs_dir: str, terms: list[dict]) -> list[dict]:
        fs = JobFilesystem(jobs_dir, job.id)
        validated = []
        for i, t in enumerate(terms):
            entry = {
                "id": t.get("id", f"t{i + 1}"),
                "term": t.get("term", "").strip(),
                "category": t.get("category", "autre"),
                "variants": t.get("variants", []),
                "priority": t.get("priority", "normale"),
                "replace_by": t.get("replace_by", "").strip(),
                "comment": t.get("comment", "").strip(),
            }
            validated.append(entry)
        fs.save_json("context/session_lexicon.json", validated)

        txt_lines = ["# Lexique de session"]
        for entry in validated:
            txt_lines.append(entry["term"])
            for v in entry.get("variants", []):
                txt_lines.append(f"# variant: {v}")
        fs.save_text("context/session_lexicon.txt", "\n".join(txt_lines))

        return validated

    @staticmethod
    def import_from_file(job: Job, jobs_dir: str, content: str) -> list[dict]:
        terms = []
        for line in content.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "," in line:
                parts = [p.strip() for p in line.split(",")]
                term = parts[0]
                category = parts[1] if len(parts) > 1 else "autre"
                priority = parts[2] if len(parts) > 2 else "normale"
            else:
                term = line
                category = "autre"
                priority = "normale"
            terms.append({"term": term, "category": category, "priority": priority, "variants": []})

        return LexiconManager.save(job, jobs_dir, terms)

    @staticmethod
    def load_global_lexicon(config: dict) -> list[str]:
        path_str = config.get("models", {}).get("global_lexicon_path", "configs/lexique_metier.txt")
        path = Path(path_str)
        if not path.is_file():
            return []
        terms = []
        for line in path.read_text(encoding="utf-8").split("\n"):
            line = line.strip()
            if line and not line.startswith("#"):
                terms.append(line)
        return terms

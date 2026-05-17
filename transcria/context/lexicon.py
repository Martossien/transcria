from pathlib import Path

from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job


LEXICON_CATEGORIES = [
    "personne", "organisation", "service", "application", "projet",
    "sigle", "métier", "technique", "produit", "statut",
    "médical", "lieu", "règlement", "finance", "montant",
    "processus", "document", "expression", "langue", "mot suspect",
]

LEXICON_PRIORITIES = ["critique", "importante", "normale"]


class LexiconManager:
    @staticmethod
    def _normalize_variants(value, term: str = "") -> list[str]:
        if isinstance(value, list):
            variants = value
        elif isinstance(value, str):
            variants = value.replace(";", ",").split(",")
        else:
            variants = []

        normalized = []
        seen = set()
        term_key = term.strip().casefold()
        empty_markers = {"aucun", "aucune", "(aucun)", "(aucune)", "néant", "neant", "n/a", "na", "-"}
        for variant in variants:
            text = str(variant).strip()
            key = text.casefold()
            if not text or key in empty_markers:
                continue
            if term_key and key == term_key:
                continue
            if key in seen:
                continue
            seen.add(key)
            normalized.append(text)
        return normalized

    @staticmethod
    def _normalize_contexts(value) -> list[dict]:
        if not isinstance(value, list):
            return []

        contexts = []
        for item in value[:3]:
            if isinstance(item, dict):
                quote = str(item.get("quote", "")).strip()
                if not quote:
                    continue
                contexts.append({
                    "variant": str(item.get("variant", "")).strip(),
                    "timecode": str(item.get("timecode", "")).strip(),
                    "speaker": str(item.get("speaker", "")).strip(),
                    "quote": quote[:500],
                    "reason": str(item.get("reason", "")).strip()[:300],
                })
            elif isinstance(item, str) and item.strip():
                contexts.append({
                    "variant": "",
                    "timecode": "",
                    "speaker": "",
                    "quote": item.strip()[:500],
                    "reason": "",
                })
        return contexts

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
            term = t.get("term", "").strip()
            entry = {
                "id": t.get("id", f"t{i + 1}"),
                "term": term,
                "category": t.get("category", "mot suspect").strip(),
                "variants": LexiconManager._normalize_variants(t.get("variants", []), term=term),
                "priority": t.get("priority", "normale"),
                "replace_by": t.get("replace_by", "").strip(),
                "comment": t.get("comment", "").strip(),
                "contexts": LexiconManager._normalize_contexts(t.get("contexts", [])),
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

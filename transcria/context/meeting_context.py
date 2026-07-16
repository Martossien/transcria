from transcria.context.meeting_type_catalog import meeting_type_names, type_specific_fields
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job

# Champs spécifiques par type de réunion — affichés dynamiquement dans le wizard
# et utilisés dans le DOCX et le contexte de correction LLM.
# SOURCE UNIQUE : transcria/data/meeting_types.yaml (via meeting_type_catalog) —
# plus aucun type/champ en dur ici (cf. docs/TYPES_REUNION_PERSONNALISES.md, lot A).
TYPE_SPECIFIC_FIELDS: dict[str, list[dict]] = type_specific_fields()

MEETING_TYPES = meeting_type_names()



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
                       "termes_suspects_parse_status", "termes_suspects_parse_warning",
                       "structured_data", "structured_data_parse_status",
                       "structured_data_parse_warning",
                       "type_specific_data"]
        for field in llm_fields:
            if field in existing and field not in context_data:
                context_data[field] = existing[field]
        merged = MeetingContextManager.default_context()
        merged.update(existing)
        merged.update(context_data)
        fs.save_json("context/meeting_context.json", merged)
        return merged

    @staticmethod
    def effective_summary_markdown(meeting_ctx: dict, raw_md: str) -> str:
        """Le summary.md EFFECTIF d'un livrable : la synthèse éditée (étape 4) ou
        harmonisée (relecture finale) remplace la section « ## Synthèse » du markdown
        brut de la LLM — même priorité que le DOCX (summary > harmonized > brut).

        Miroir de l'extraction du wizard (job_wizard.html, textarea de l'étape 4) :
        si le brut n'a pas de section « ## Synthèse », l'édition remplace tout.
        """
        effective = str(meeting_ctx.get("summary") or meeting_ctx.get("summary_harmonized") or "").strip()
        raw = raw_md or ""
        if not effective:
            return raw
        # Différé : cycle d'__init__ de paquets — gpu/ importe context/ en tête (prompts du runner LLM).
        from transcria.gpu.opencode_runner import summary_markers

        # Marqueur de la section synthèse selon la langue des livrables (Axe B ; défaut « ## Synthèse »).
        marker = summary_markers(meeting_ctx.get("language"))["summary_heading"]
        if marker not in raw:
            return effective + "\n"
        head, _, tail = raw.partition(marker)
        rest = tail.split("\n##", 1)
        replaced = head + marker + "\n\n" + effective + "\n"
        if len(rest) > 1:
            replaced += "\n##" + rest[1]
        return replaced

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

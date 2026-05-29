from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job

# Champs spécifiques par type de réunion — affichés dynamiquement dans le wizard
# et utilisés dans le DOCX et le contexte de correction LLM.
TYPE_SPECIFIC_FIELDS: dict[str, list[dict]] = {
    "CSE": [
        {"key": "president_seance",  "label": "Président de séance",          "type": "text"},
        {"key": "secretaire_seance", "label": "Secrétaire de séance",          "type": "text"},
        {"key": "membres_presents",  "label": "Membres titulaires présents",   "type": "number"},
        {"key": "membres_total",     "label": "Membres titulaires (total)",     "type": "number"},
        {"key": "ref_pv_precedent",  "label": "Réf. PV précédent",             "type": "text"},
    ],
    "CSE extraordinaire": [
        {"key": "president_seance",  "label": "Président de séance",              "type": "text"},
        {"key": "secretaire_seance", "label": "Secrétaire de séance",              "type": "text"},
        {"key": "membres_presents",  "label": "Membres titulaires présents",       "type": "number"},
        {"key": "membres_total",     "label": "Membres titulaires (total)",         "type": "number"},
        {"key": "objet_seance",      "label": "Objet de la séance extraordinaire", "type": "textarea"},
    ],
    "Point projet": [
        {"key": "nom_projet",     "label": "Nom du projet",    "type": "text"},
        {"key": "phase_jalon",    "label": "Phase / Jalon",    "type": "text"},
        {"key": "chef_de_projet", "label": "Chef de projet",   "type": "text"},
        {"key": "sprint",         "label": "Sprint n°",        "type": "text"},
    ],
    "CODIR / COMEX": [
        {"key": "ordre_du_jour_items", "label": "Ordre du jour (un point par ligne)", "type": "textarea"},
    ],
    "Réunion client": [
        {"key": "nom_client",  "label": "Nom du client",           "type": "text"},
        {"key": "ref_contrat", "label": "Référence contrat / offre", "type": "text"},
    ],
    "Entretien individuel": [
        {"key": "periode_evaluee", "label": "Période d'évaluation",  "type": "text"},
        {"key": "poste_evalue",    "label": "Poste de l'évalué(e)",  "type": "text"},
        {"key": "evaluateur",      "label": "Évaluateur(trice)",     "type": "text"},
    ],
    "Formation": [
        {"key": "formateur",                "label": "Formateur / Organisme",     "type": "text"},
        {"key": "nb_participants_formation", "label": "Nombre de participants",    "type": "number"},
        {"key": "lieu_formation",            "label": "Lieu / distanciel",        "type": "text"},
    ],
    "Réunion de crise": [
        {"key": "nature_incident",   "label": "Nature de l'incident",  "type": "text"},
        {"key": "responsable_crise", "label": "Responsable de crise",  "type": "text"},
    ],
    "Séminaire / atelier": [
        {"key": "thematique", "label": "Thématique principale",       "type": "text"},
        {"key": "nb_groupes", "label": "Nombre de groupes de travail", "type": "number"},
    ],
    "Négociation": [
        {"key": "objet_negociation", "label": "Objet de la négociation", "type": "text"},
        {"key": "parties",           "label": "Parties prenantes",       "type": "text"},
    ],
}

MEETING_TYPES = [
    # Types existants — comportement inchangé (template DOCX v1)
    "Réunion interne",
    "Réunion projet",
    "Réunion technique",
    "Formation",
    "Réunion médicale / santé",
    "RH",
    "Entretien",
    # Types v2 — sections enrichies selon données extraites
    "CSE",
    "CSE extraordinaire",
    "CODIR / COMEX",
    "Réunion client",
    "Point projet",
    "Réunion de crise",
    "Séminaire / atelier",
    "Négociation",
    "Entretien individuel",
    "Podcast / média",
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

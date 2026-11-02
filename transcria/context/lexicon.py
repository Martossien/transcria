import re

from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job

LEXICON_CATEGORIES = [
    "personne", "organisation", "service", "application", "projet",
    "sigle", "métier", "technique", "produit", "statut",
    "médical", "lieu", "règlement", "finance", "montant",
    "processus", "document", "expression", "langue", "mot suspect",
]

LEXICON_PRIORITIES = ["critique", "importante", "normale"]

# Synonymes acceptés à la saisie / à l'import (mesure 2026-11 sur 17 jobs réels : 14 entrées
# portaient « normal » ou « important » — rabattues en silence sur « normale », elles
# perdaient la préservation que l'utilisateur croyait avoir choisie).
_PRIORITY_SYNONYMS = {
    "critique": "critique", "critical": "critical", "critica": "critique", "crítica": "critique",
    "kritisch": "critique", "crítico": "critique", "critico": "critique",
    "importante": "importante", "important": "importante", "wichtig": "importante",
    "normale": "normale", "normal": "normale", "standard": "normale",
}
_PRIORITY_SYNONYMS["critical"] = "critique"


def normalize_priority(value: object) -> str:
    """Priorité canonique (``critique`` / ``importante`` / ``normale``) — synonymes tolérés,
    inconnue ⇒ ``normale``."""
    text = str(value or "").strip().lower()
    return _PRIORITY_SYNONYMS.get(text, "normale")

# Affichage localisé des priorités (axe A). La VALEUR reste la clé canonique FR (stockée,
# comparée : _PRIORITY_RANK, keep_priorities…) ; seul le libellé visible est traduit. fr =
# identité stricte. Même principe que les profils/types de réunion.
_LEXICON_PRIORITY_LABELS_EN = {"critique": "critical", "importante": "important", "normale": "normal"}
# Une locale absente retombe sur la clé FR — ajouter une langue = ajouter son dict ici.
_LEXICON_PRIORITY_LABELS: dict[str, dict[str, str]] = {
    "en": _LEXICON_PRIORITY_LABELS_EN,
    "de": {"critique": "kritisch", "importante": "wichtig", "normale": "normal"},
    "es": {"critique": "crítica", "importante": "importante", "normale": "normal"},
    "it": {"critique": "critica", "importante": "importante", "normale": "normale"},
    "pt": {"critique": "crítica", "importante": "importante", "normale": "normal"},
}


def localized_priority(key: str, language: str | None) -> str:
    """Libellé d'affichage d'une priorité de lexique (repli = clé FR inchangée)."""
    table = _LEXICON_PRIORITY_LABELS.get(language or "fr")
    return table.get(key, key) if table else key


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
                    "listened": bool(item.get("listened", False)),
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
                "category": t.get("category", "mot suspect").strip() or "mot suspect",
                "variants": LexiconManager._normalize_variants(t.get("variants", []), term=term),
                "priority": normalize_priority(t.get("priority", "normale")),
                "replace_by": t.get("replace_by", "").strip(),
                "comment": t.get("comment", "").strip(),
                "contexts": LexiconManager._normalize_contexts(t.get("contexts", [])),
            }
            # Mentions relevées par la LLM (« semestre 2 » pour « S2 ») : gardées pour le
            # contexte et la présence, JAMAIS des cibles de remplacement — distinctes des variantes.
            mentions = [m for m in LexiconManager._normalize_variants(t.get("mentions", []), term=term)
                        if m.casefold() not in {v.casefold() for v in entry["variants"]}]
            if mentions:
                entry["mentions"] = mentions
            for optional in ("source", "central_entry_id", "central_lexicon_id", "central_lexicon_name", "_display_reason"):
                if t.get(optional):
                    entry[optional] = str(t.get(optional)).strip()
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
                category = parts[1] if len(parts) > 1 else "mot suspect"
                priority = parts[2] if len(parts) > 2 else "normale"
            else:
                term = line
                category = "mot suspect"
                priority = "normale"
            terms.append({"term": term, "category": category, "priority": priority, "variants": []})

        return LexiconManager.save(job, jobs_dir, terms)


def apply_validated_forms(text: str, entries: list | None) -> str:
    """Remplace, dans un texte LIBRE, chaque variante d'un lexique VALIDÉ par sa forme de
    référence (``replace_by`` sinon ``term``) — mot entier, insensible à la casse.

    Mesuré 2026-11-02 sur 65 jobs réels : 29 jobs portaient dans un rôle de participant une
    variante que l'utilisateur avait lui-même déclarée fautive au lexique (109 champs, 98 fois
    le texte de la LLM de résumé jamais retouché) — le tableau du rapport Word l'imprimait
    telle quelle. Les variantes sont par définition des graphies fautives validées comme
    telles : aucun homographe légitime à préserver. Les variantes les plus longues passent en
    premier (« SIGLE_A B » avant « SIGLE_A »). Jamais appliqué à un artefact stocké : rendu seul.
    """
    if not text or not entries:
        return text
    pairs: list[tuple[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        form = str(entry.get("replace_by") or entry.get("term") or "").strip()
        if not form:
            continue
        for variant in entry.get("variants") or []:
            v = str(variant).strip()
            if v and v.lower() != form.lower():
                pairs.append((v, form))
    if not pairs:
        return text
    out = text
    for variant, form in sorted(pairs, key=lambda vf: -len(vf[0])):
        out = re.sub(r"(?<!\w)" + re.escape(variant) + r"(?!\w)", form, out, flags=re.IGNORECASE)
    return out

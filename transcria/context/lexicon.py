import re
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


#: Une forme validée « A / B » (deux graphies proposées, jamais tranchées) ne pilote aucune
#: substitution — mesuré avant d'écrire ceci : 12/30 entrées d'une réunion réelle, et des
#: remplacements qui rendaient la phrase fausse (cf. workflow/phases/correction.py, 2026-08-22).
_FORME_AMBIGUE = re.compile(r"\s/\s")


def sans_formes_ambigues(lexicon: list) -> tuple[list, int]:
    """Retire du lexique de SUBSTITUTION les entrées dont la forme validée est ambiguë (« A / B »).
    L'entrée reste dans le lexique de session relu par l'humain ; elle perd seulement le pouvoir
    de déclencher un remplacement — en code, pas en consigne (deux consignes de prompt n'ont pas tenu)."""
    gardees = [t for t in lexicon
               if not (isinstance(t, dict) and _FORME_AMBIGUE.search(str(t.get("term", ""))))]
    return gardees, len(lexicon) - len(gardees)


def sans_variantes_expansions(lexicon: list) -> tuple[list, int]:
    """Retire les variantes MULTI-MOTS (≥ 3) des cibles de substitution d'un sigle : remplacer toute
    une locution prononcée par le sigle efface des mots réels (constaté sur trois parcours réels,
    2026-08-24). L'ENTRÉE reste, avec ses variantes courtes."""
    epurees, retirees = [], 0
    for entree in lexicon:
        if not isinstance(entree, dict):
            epurees.append(entree)
            continue
        term = str(entree.get("term", ""))
        variants = entree.get("variants") or []
        if len(term) <= 10 and term.isupper() and isinstance(variants, list):
            gardees = [v for v in variants if len(str(v).split()) < 3]
            if len(gardees) != len(variants):
                retirees += len(variants) - len(gardees)
                entree = {**entree, "variants": gardees}
        epurees.append(entree)
    return epurees, retirees


def substitution_entries(entries: list | None, *, verbatim: bool = False) -> list[dict]:
    """Les entrées qui ont le DROIT de remplacer : formes non ambiguës, avec au moins une variante
    distincte de la forme validée. ``verbatim=True`` (transcription) retire aussi les expansions
    multi-mots des sigles — remplacer une locution PRONONCÉE par le sigle effacerait des mots réels ;
    dans un texte rédigé (titre, synthèse, rôle) la forme validée s'applique telle quelle. Règles
    IDENTIQUES à celles du lexique transmis à la LLM de correction : une seule source de vérité."""
    if not entries:
        return []
    kept, _ = sans_formes_ambigues([e for e in entries if isinstance(e, dict)])
    if verbatim:
        kept, _ = sans_variantes_expansions(kept)
    out: list[dict] = []
    for e in kept:
        form = str(e.get("replace_by") or e.get("term") or "").strip()
        variants = [str(v).strip() for v in (e.get("variants") or []) if str(v).strip()]
        if form and any(v.lower() != form.lower() for v in variants):
            out.append(e)
    return out


def load_session_lexicon(job_dir: str | Path) -> list[dict]:
    """Lexique de session d'un job (``context/session_lexicon.json``), ``[]`` si absent/illisible —
    jamais d'exception : un filet qui casserait un rendu ne protège rien."""
    import json

    try:
        data = json.loads((Path(job_dir) / "context" / "session_lexicon.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def count_remaining_variants(text: str, entries: list | None, *, verbatim: bool = False) -> int:
    """Occurrences (mot entier, casse ignorée) de variantes AUTORISÉES à la substitution encore
    présentes dans ``text`` — la mesure que le livrable doit ramener à zéro."""
    if not text:
        return 0
    total = 0
    for e in substitution_entries(entries, verbatim=verbatim):
        form = str(e.get("replace_by") or e.get("term") or "").strip()
        for v in e.get("variants") or []:
            v = str(v).strip()
            if v and v.lower() != form.lower():
                total += len(re.findall(r"(?<!\w)" + re.escape(v) + r"(?!\w)", text, flags=re.IGNORECASE))
    return total


_SRT_TIMECODE = re.compile(r"^\d{2}:\d{2}:\d{2}[,.]\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}[,.]\d{3}")
_SRT_SPEAKER_PREFIX = re.compile(r"^((?:SPEAKER|PISTE)_[A-Za-z0-9_]+(?:\([^)]*\))?\s*:\s*)")


def harmonize_srt(srt_text: str, entries: list | None) -> tuple[str, int]:
    """Filet DÉTERMINISTE sur un SRT : chaque variante validée fautive est remplacée par sa forme
    validée dans le TEXTE des sous-titres seulement — jamais les index, les timecodes ni le préfixe
    ``SPEAKER_XX(Nom):`` (mapping locuteurs intact). Rend (texte, nombre de remplacements).

    Vécu 2026-11-07 (job 9b63a5c7) : la LLM de correction avait laissé 3 variantes sur 38 et la
    relecture finale en a réintroduit ; dix-sept commits de prompts n'avaient jamais rendu cette
    application certaine. Un lexique validé est une consigne : le code l'applique, la LLM juge le reste.
    """
    allowed = substitution_entries(entries, verbatim=True)
    if not srt_text or not allowed:
        return srt_text, 0
    before = count_remaining_variants(srt_text, allowed, verbatim=True)
    if before == 0:
        return srt_text, 0
    out_lines: list[str] = []
    for line in srt_text.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.isdigit() or _SRT_TIMECODE.match(stripped):
            out_lines.append(line)
            continue
        m = _SRT_SPEAKER_PREFIX.match(line)
        prefix, body = (m.group(1), line[m.end():]) if m else ("", line)
        out_lines.append(prefix + apply_validated_forms(body, allowed))
    out = "\n".join(out_lines)
    return out, before - count_remaining_variants(out, allowed, verbatim=True)


_CONTEXT_TEXT_FIELDS = ("title", "title_suggere", "topic", "sujet_suggere", "objective", "objectif_suggere",
                        "notes", "notes_suggeres", "summary", "summary_machine", "summary_harmonized", "summary_llm")


def harmonize_context_fields(meeting_ctx: dict | None, entries: list | None) -> dict:
    """COPIE du contexte de réunion dont les champs TEXTE portent les formes validées (titre, sujet,
    objectif, ordre du jour, synthèses) — pour le RENDU des livrables ; le fichier reste intact.
    La synthèse éditée par l'humain est harmonisée aussi : le lexique est validé APRÈS l'étape 4,
    et une variante qu'il déclare fautive l'est dans tout le document (vécu : « Théo »/« TéHOU »
    mélangés dans le DOCX, titre compris)."""
    if not isinstance(meeting_ctx, dict):
        return {}
    allowed = substitution_entries(entries)
    if not allowed:
        return dict(meeting_ctx)
    out = dict(meeting_ctx)
    for key in _CONTEXT_TEXT_FIELDS:
        value = out.get(key)
        if isinstance(value, str) and value:
            out[key] = apply_validated_forms(value, allowed)
    return out


def harmonize_structured(value, entries: list | None):
    """Même filet sur les données structurées (décisions, actions, votes… : chaînes dans des listes
    et dictionnaires, récursif) — rend une COPIE ; les clés ne sont jamais touchées."""
    allowed = substitution_entries(entries)
    if not allowed:
        return value

    def walk(v):
        if isinstance(v, str):
            return apply_validated_forms(v, allowed)
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        return v
    return walk(value)


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
    for entry in substitution_entries(entries):
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

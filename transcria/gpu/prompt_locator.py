"""Politique de langue des livrables et résolution des prompts (vague C2).

Corps extraits d'``opencode_runner`` : fonctions pures de (config, job, langue) —
aucun sous-processus, aucun état. ``opencode_runner`` les ré-exporte (les
consommateurs historiques — phases, web, exports, quality — importent chez lui).
"""
import os

_LANGUAGE_NAMES = {
    "fr": "français", "en": "English", "de": "Deutsch", "it": "italiano", "es": "español",
}


def _get_prompts_dir(config: dict | None = None) -> str:
    if config:
        custom = config.get("workflow", {}).get("prompts_dir")
        if custom:
            return custom
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "..", "configs", "prompts",
    )


# Langue des LIVRABLES générés (Axe B). Réutilise ``meeting_context.language`` (choisi à
# l'étape Contexte, défaut « fr »). Les prompts localisés vivent dans
# ``configs/prompts/<lang>/`` ; en leur absence on retombe sur la racine (français source),
# ce qui garantit la non-régression pour les jobs existants.
def resolve_output_language(job=None, extra_data: dict | None = None) -> str:
    """Langue cible des livrables (STT, résumé, prompts, DOCX, rapports).

    Ordre de résolution :
      1. langue EXPLICITE du job (``meeting_context.language`` dans extra_data — posée par le
         formulaire de contexte de l'étape 3 ou la détection LLM) ;
      2. à défaut, la locale du PROPRIÉTAIRE (= la langue d'interface qu'il a choisie) : si
         l'utilisateur travaille en anglais, ses jobs sortent en anglais PAR DÉFAUT (sinon la
         passe STT rapide, qui tourne AVANT l'étape 3, franciserait un audio anglais) ;
      3. à défaut, « fr » (source historique).

    Défensif : un objet job sans ``get_extra_data``/``owner`` (doublure de test) ou un owner
    détaché de session → « fr »."""
    if extra_data is not None:
        data = extra_data
    else:
        getter = getattr(job, "get_extra_data", None)
        data = getter() if callable(getter) else {}
    explicit = ((data or {}).get("meeting_context", {}) or {}).get("language")
    if explicit:
        return str(explicit)
    try:
        owner = getattr(job, "owner", None)
        owner_locale = getattr(owner, "locale", None) if owner is not None else None
    except Exception:  # noqa: BLE001 — owner détaché de session hors requête : repli sûr
        owner_locale = None
    # N'accepter qu'une vraie chaîne (une doublure de test peut exposer un Mock tronqué).
    if isinstance(owner_locale, str) and owner_locale:
        return owner_locale
    return "fr"


def resolve_prompt_file(config: dict | None, filename: str, language: str = "fr") -> str:
    """Chemin absolu du prompt pour ``language`` : ``<prompts_dir>/<lang>/<filename>`` s'il
    existe, sinon repli sur ``<prompts_dir>/<filename>`` (français source)."""
    base = _get_prompts_dir(config)
    localized = os.path.abspath(os.path.join(base, language, filename))
    if language and language != "fr" and os.path.isfile(localized):
        return localized
    return os.path.abspath(os.path.join(base, filename))


def language_directive(language: str) -> str:
    """Consigne EXPLICITE de langue de sortie injectée dans l'instruction LLM.

    Robustesse (Axe B, bêta) : en plus du prompt localisé éventuel, on ne laisse jamais le
    modèle deviner la langue. On lui demande de rédiger le CONTENU rédactionnel (synthèse,
    reformulations, valeurs de champs) dans la langue cible, tout en **préservant à
    l'identique les en-têtes de structure et marqueurs de format** (ex. ``## Synthèse``,
    balises, noms de clés) — ils sont lus par le code : les traduire casserait le parsing."""
    if not language or language == "fr":
        return ""
    name = _LANGUAGE_NAMES.get(language, language)
    return (
        f"IMPORTANT (langue des livrables) : rédige le CONTENU rédactionnel (synthèse, "
        f"reformulations, valeurs de champs) en {name}. Emploie EXACTEMENT les en-têtes de "
        f"structure et marqueurs de format spécifiés par le prompt (ils sont lus par le code) ; "
        f"conserve les noms propres et les termes du glossaire. "
    )


# ── Contrat de marqueurs du résumé, par langue (Axe B) ──────────────────────
# Les entrées `fr` reproduisent À L'IDENTIQUE les marqueurs historiques (non-régression
# prouvée par test). Les entrées `en` sont le contrat que le prompt EN doit respecter :
# ``configs/prompts/en/summary_prompt.txt`` DOIT produire ces marqueurs, et le parser les
# lit ici. Correction / relecture finale n'apparaissent pas : elles lisent des fichiers à
# noms fixes (SRT + .md), donc neutres en langue.
_SUMMARY_MARKERS: dict[str, dict[str, str]] = {
    "fr": {
        "title": "Titre suggéré",
        "type": "Type suggéré",
        "subject": "Sujet principal",
        "objective": "Objectif probable",
        "notes": "Notes / Ordre du jour probable",
        "keywords": "Mots-clés",
        "participant_count": "Nombre de participants détectés",
        "participants_heading": "## Participants probables",
        "terms_section_re": r"Termes\s+(?:suspects|douteux)[^\n]*",
        "structured_section_re": r"Données\s+structurées",
        "summary_heading": "## Synthèse",
    },
    "en": {
        "title": "Suggested title",
        "type": "Suggested type",
        "subject": "Main topic",
        "objective": "Probable objective",
        "notes": "Notes / Probable agenda",
        "keywords": "Keywords",
        "participant_count": "Number of detected participants",
        "participants_heading": "## Probable participants",
        "terms_section_re": r"(?:Doubtful|Suspect)\s+terms[^\n]*",
        "structured_section_re": r"Structured\s+data",
        "summary_heading": "## Summary",
    },
}


def summary_markers(language: str | None) -> dict[str, str]:
    """Marqueurs du résumé pour ``language`` (repli français si langue inconnue)."""
    return _SUMMARY_MARKERS.get((language or "fr"), _SUMMARY_MARKERS["fr"])


def build_harmonization_glossary(participants: list, lexicon: list) -> str:
    """Construit le glossaire validé (Markdown) pour harmoniser la synthèse.

    Fonction pure : agrège les **noms de participants validés** et les **formes
    canoniques du lexique** (avec variantes connues) en un glossaire compact que la
    LLM applique en contexte sur la synthèse produite avant correction. Retourne une
    chaîne vide si aucune donnée exploitable.
    """
    names: list[str] = []
    for entry in participants or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        if name and name not in names:
            names.append(name)

    terms: list[str] = []
    for entry in lexicon or []:
        if not isinstance(entry, dict):
            continue
        target = str(entry.get("replace_by", "")).strip() or str(entry.get("term", "")).strip()
        if not target:
            continue
        variants = [str(v).strip() for v in (entry.get("variants") or []) if str(v).strip()]
        line = f"- {target}" + (f" ← {', '.join(variants)}" if variants else "")
        if line not in terms:
            terms.append(line)

    if not names and not terms:
        return ""

    lines = ["# Glossaire validé (à appliquer en contexte sur la synthèse)", ""]
    if names:
        lines.append("## Noms de participants (orthographe validée)")
        lines.extend(f"- {name}" for name in names)
        lines.append("")
    if terms:
        lines.append("## Termes métier (forme validée ← variantes connues)")
        lines.extend(terms)
    return "\n".join(lines).strip() + "\n"

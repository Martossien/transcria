"""Variables de prompts des types de réunion (lot D — docs/TYPES_REUNION_PERSONNALISES.md §4).

Le prompt de résumé (``configs/prompts/summary_prompt.txt``) porte trois placeholders,
substitués À LA CONSTRUCTION DE L'INSTRUCTION (le fichier versionné reste éditable
dans l'admin, le contrat de parsing est inchangé) :

- ``{{TYPES_REUNION}}``          — la liste des types pour le « Type suggéré » du gabarit
  (intégrés + personnalisés VISIBLES DU PROPRIÉTAIRE du job) ;
- ``{{INDICES_TYPES}}``          — les indices de sélection (§ 8), depuis le catalogue ;
- ``{{CHAMPS_EXTRACTION_TYPE}}`` — les champs d'extraction du type CHOISI (fiche
  matérialisée) : injectés aux RELANCES de résumé et à la relecture finale seulement
  (P1 tranché — au 1ᵉʳ résumé le type n'est pas encore choisi, le bloc est vide).

**Compatibilité** : un prompt personnalisé par un admin SANS placeholder reste
fonctionnel — la substitution est un no-op et le comportement historique demeure.
Règle absolue : les valeurs injectées viennent du catalogue/des fiches VALIDÉES
(bornes anti-injection posées par ``validate_type_definition``), jamais d'un texte libre.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from transcria.context.meeting_type_catalog import detection_hints, meeting_type_names

if TYPE_CHECKING:  # pragma: no cover
    from transcria.auth.models import User

PLACEHOLDER_TYPES = "{{TYPES_REUNION}}"
PLACEHOLDER_HINTS = "{{INDICES_TYPES}}"
PLACEHOLDER_EXTRACT = "{{CHAMPS_EXTRACTION_TYPE}}"


def _hint_line(name: str, hints: list[str]) -> str:
    quoted = ", ".join(f'"{h}"' for h in hints)
    return f"  `{name}` si on entend {quoted} ;"


def build_prompt_substitutions(owner: "User | None" = None,
                               chosen_custom_type: dict | None = None) -> dict[str, str]:
    """Valeurs des trois placeholders pour un job donné.

    ``owner`` (propriétaire du job) élargit la liste aux types personnalisés qu'il
    voit — sans lui (contexte hors requête/DB), le catalogue intégré seul est servi.
    ``chosen_custom_type`` = la fiche matérialisée (``meeting_context["custom_type"]``).
    """
    names = list(meeting_type_names())
    hints: dict[str, list[str]] = dict(detection_hints())
    if owner is not None:
        from transcria.context.meeting_type_store import MeetingTypeStore

        for template in MeetingTypeStore.visible_templates_for_user(owner):
            names.append(template.name)
            template_hints = template.definition.get("detection_hints") or []
            if template_hints:
                hints[template.name] = list(template_hints)

    indices_lines = [_hint_line(name, hints[name]) for name in names if name in hints]

    extract_block = ""
    extract_fields = (chosen_custom_type or {}).get("extract_fields") or []
    if extract_fields:
        lines = [
            "Champs SUPPLÉMENTAIRES demandés par le type de réunion choisi — mêmes règles",
            "strictes (présence explicite, `[]` si absent, ZÉRO INVENTION), à ajouter au",
            "MÊME bloc JSON :",
        ]
        for field in extract_fields:
            lines.append(f'- "{field["key"]}" : {field["instruction"]} (libellé : {field["label"]})')
        extract_block = "\n".join(lines)

    return {
        PLACEHOLDER_TYPES: " | ".join(names),
        PLACEHOLDER_HINTS: "\n".join(indices_lines),
        PLACEHOLDER_EXTRACT: extract_block,
    }


def substitute_placeholders(text: str, substitutions: dict[str, str]) -> str:
    """Remplace les placeholders présents — texte sans placeholder = no-op strict."""
    for placeholder, value in substitutions.items():
        if placeholder in text:
            text = text.replace(placeholder, value)
    return text

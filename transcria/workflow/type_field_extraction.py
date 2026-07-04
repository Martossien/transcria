"""Extraction LÉGÈRE des champs sur-mesure d'un type de réunion — logique pure.

Trou identifié à l'analyse macro (2026-07-04) : les ``extract_fields`` d'un type de
réunion personnalisé ne sont peuplés que par la relecture finale (``run_final_review``)
ou une relance du résumé. Les profils qui font le RÉSUMÉ mais PAS la relecture finale
— au premier chef **Word structuré** (``contexte=required``, ``final_review=False``) —
laissent donc ces champs vides dans le livrable, silencieusement.

Cette micro-étape comble le trou avec un prompt COURT dédié (juste les champs
demandés), bien plus léger que la relecture finale complète (A+C+D+G). Elle ne tourne
QUE quand un type avec ``extract_fields`` est choisi ET que le profil ne fait pas de
relecture finale — coût GPU nul pour tous les autres cas.

Ici : construction du message + fusion du résultat (pur, testé). L'orchestration LLM
+ VRAM est dans ``WorkflowRunner.run_type_field_extraction``.
"""
from __future__ import annotations

import json
import re

# Réutilise le nettoyage des blocs <think> (certains templates les renvoient inline).
_THINK_BLOCK = re.compile(r"(?s)<think>.*?</think>")
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def extract_fields_from_type(custom_type: dict | None) -> list[dict]:
    """Les ``extract_fields`` VALIDES d'une fiche de type matérialisée (clé + instruction)."""
    fields = (custom_type or {}).get("extract_fields") or []
    return [f for f in fields if isinstance(f, dict) and f.get("key") and f.get("instruction")]


def build_extraction_messages(*, transcript: str, extract_fields: list[dict],
                              max_transcript_chars: int = 60000) -> list[dict]:
    """Messages OpenAI-chat pour l'extraction ciblée. Prompt court : uniquement les
    champs demandés, présence explicite, ZÉRO invention, réponse JSON stricte."""
    if max_transcript_chars > 0 and len(transcript) > max_transcript_chars:
        transcript = transcript[:max_transcript_chars] + "\n[… transcription tronquée …]"

    field_lines = "\n".join(
        f'- "{f["key"]}" : {f["instruction"]} (libellé : {f.get("label", f["key"])})'
        for f in extract_fields
    )
    keys = [f["key"] for f in extract_fields]
    system = (
        "Tu extrais des champs PRÉCIS d'une transcription de réunion, RIEN DE PLUS.\n"
        "Règles STRICTES :\n"
        "- présence EXPLICITE uniquement : n'extrais que ce qui est réellement dit ;\n"
        "- ZÉRO INVENTION : si un champ est absent de la transcription, rends une liste vide `[]` ;\n"
        "- réponds UNIQUEMENT par un objet JSON, sans texte autour, avec EXACTEMENT ces clés :\n"
        f"{json.dumps(keys, ensure_ascii=False)}\n"
        "- chaque valeur est une LISTE de chaînes (une par élément trouvé).\n\n"
        "Champs à extraire :\n" + field_lines
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Transcription :\n\n{transcript}\n\nRends le JSON demandé."},
    ]


def parse_extracted_fields(llm_response: str, extract_fields: list[dict]) -> dict:
    """Extrait le JSON de la réponse, ne garde QUE les clés demandées, normalise en
    listes de chaînes. Une réponse illisible → tous les champs vides (jamais d'exception)."""
    keys = [f["key"] for f in extract_fields]
    result: dict[str, list[str]] = {k: [] for k in keys}
    if not llm_response:
        return result
    cleaned = _THINK_BLOCK.sub("", llm_response).strip()
    match = _JSON_BLOCK.search(cleaned)
    if not match:
        return result
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return result
    if not isinstance(data, dict):
        return result
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            result[key] = [str(v).strip() for v in value if str(v).strip()]
        elif isinstance(value, str) and value.strip():
            result[key] = [value.strip()]
        # tout autre type (None, nombre, dict) → liste vide (ZÉRO invention préservé)
    return result


def merge_into_structured_data(structured_data: dict, extracted: dict) -> tuple[dict, list[str]]:
    """Fusionne les champs extraits NON VIDES dans ``structured_data`` (les clés
    existantes non vides ne sont pas écrasées par du vide). Renvoie (data, clés_ajoutées)."""
    merged = dict(structured_data or {})
    added: list[str] = []
    for key, values in extracted.items():
        if values and not merged.get(key):
            merged[key] = values
            added.append(key)
    return merged, added

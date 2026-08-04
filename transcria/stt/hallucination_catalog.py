"""Catalogue des signatures d'hallucination ASR par moteur (config-driven).

Charge ``transcria/data/hallucination_signatures.yaml`` : la clé ``generic`` vaut pour
tous les moteurs, chaque autre clé est un identifiant de backend (``models.stt_backend``).
Une signature invalide (regex, action inconnue) est IGNORÉE avec un avertissement —
un catalogue partiellement faux ne doit jamais casser une transcription.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_CATALOG_PATH = Path(__file__).resolve().parents[1] / "data" / "hallucination_signatures.yaml"
_VALID_ACTIONS = frozenset({"flag", "delete"})


@dataclass(frozen=True)
class Signature:
    regex: re.Pattern
    action: str          # "flag" | "delete"
    source: str          # "generic" ou l'identifiant du moteur — pour la traçabilité


@lru_cache(maxsize=1)
def _load_raw() -> dict:
    try:
        data = yaml.safe_load(_CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Catalogue d'hallucinations illisible (%s) : %s", _CATALOG_PATH, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _compile_entries(entries: object, source: str) -> tuple[Signature, ...]:
    signatures: list[Signature] = []
    if not isinstance(entries, list):
        return ()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        pattern = entry.get("pattern")
        action = str(entry.get("action") or "flag")
        if action not in _VALID_ACTIONS:
            logger.warning("Signature %s[%d] : action inconnue « %s » — ignorée", source, index, action)
            continue
        if not isinstance(pattern, str) or not pattern.strip():
            continue
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            logger.warning("Signature %s[%d] : regex invalide (%s) — ignorée", source, index, exc)
            continue
        signatures.append(Signature(regex=regex, action=action, source=source))
    return tuple(signatures)


@lru_cache(maxsize=16)
def signatures_for_backend(backend: str | None) -> tuple[Signature, ...]:
    """Signatures applicables à ce moteur : ``generic`` + les siennes propres."""
    raw = _load_raw()
    result = list(_compile_entries(raw.get("generic"), "generic"))
    if backend and backend in raw:
        result.extend(_compile_entries(raw.get(backend), backend))
    return tuple(result)


def match_signature(text: str, backend: str | None) -> Signature | None:
    """Première signature qui matche le texte — ``delete`` prioritaire sur ``flag``."""
    if not text:
        return None
    matched = [s for s in signatures_for_backend(backend) if s.regex.search(text)]
    if not matched:
        return None
    return next((s for s in matched if s.action == "delete"), matched[0])

"""Socle commun des vérifications doctor — résultat, statuts, traducteur, résolveurs.

Découpe de la vague 0 (2026-07) : `transcria/diagnostics/doctor.py` (1 600 lignes) devient un
paquet par domaine. Ce module porte ce que TOUS les domaines partagent ; il n'importe aucun
domaine (pas de cycle possible). La façade `transcria.diagnostics.doctor` ré-exporte tout —
les appelants existants ne changent pas.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from transcria.cli_i18n import make_translator
from transcria.diagnostics.doctor_messages import DOCTOR_MESSAGES

_t = make_translator(DOCTOR_MESSAGES)

OK = "ok"

WARN = "warn"

FAIL = "fail"

_SYMBOLS = {OK: "✓", WARN: "⚠", FAIL: "✗"}

_LABELS = {OK: "OK", WARN: "WARN", FAIL: "FAIL"}

EXIT_OK = 0

EXIT_FAIL = 1

_VALID_PROFILES = ("all-in-one", "web", "scheduler", "resource-node", "migrate")

# SOURCE UNIQUE des modules de modèles (cf. transcria.database.MODEL_MODULES) : le diff de
# schéma à chaud doit peupler db.metadata avec TOUTES les tables, sinon une table réelle
# (ex. job_timing, meeting_type_templates) est vue « en trop » ou son absence non détectée.

@dataclass
class CheckResult:
    """Résultat d'une vérification. ``status`` ∈ {ok, warn, fail}."""

    name: str
    status: str
    detail: str
    hint: str | None = None

def _resolve_database_uri(cfg: dict) -> str:
    return (
        os.environ.get("TRANSCRIA_DATABASE_URL")
        or cfg.get("storage", {}).get("database_url")
        or "sqlite:///transcrIA.db"
    )

def _redact_uri(uri: str) -> str:
    try:
        from sqlalchemy.engine.url import make_url

        return make_url(uri).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001
        return uri

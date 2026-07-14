"""Petits utilitaires de requête partagés par les modules de routes web (vague A2)."""
import re
from urllib.parse import urlparse

from flask import jsonify, request

DEFAULT_JOB_TITLE = "Réunion sans titre"


def json_body(expected: type):
    """Corps JSON d'une API, TYPÉ et tolérant (banc fuzz C0.2).

    - corps absent / null / JSON invalide → valeur vide du type attendu (comportement
      historique de ``request.get_json() or {}``), jamais de page HTML 400 ;
    - corps du MAUVAIS type racine (ex. une chaîne) → (None, 400 JSON propre) au lieu
      d'un AttributeError 500 sur ``data.get``.
    """
    data = request.get_json(silent=True)
    if data is None:
        return (expected(), None)
    if not isinstance(data, expected):
        attendu = "objet" if expected is dict else "liste"
        return (None, (jsonify({"error": f"Corps JSON invalide : {attendu} attendu."}), 400))
    return (data, None)


def clean_job_title(title: str | None, default: str = DEFAULT_JOB_TITLE) -> str:
    cleaned = re.sub(r"[\x00-\x1f\x7f<>]", "", title or "").strip()
    return (cleaned or default)[:255]


def audit_origin_from_url(value: str | None) -> str:
    parsed = urlparse(value or "")
    if not parsed.hostname:
        return ""
    if parsed.port:
        return f"{parsed.hostname}:{parsed.port}"
    return parsed.hostname

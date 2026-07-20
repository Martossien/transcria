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


def api_stable(view):
    """Marque une route comme CONTRAT scriptable (vague C8).

    Le parcours upload → process → status → download est ce que les
    auto-hébergeurs peuvent scripter : rendu ⭐ dans docs/API_REFERENCE.md.
    Tout le reste est interne et peut bouger sans préavis."""
    view.__api_stable__ = True
    return view


def bearer_token_allowed(view):
    """Accepte `Authorization: Bearer tia_…` sur une route ⭐ (identité lot 4, §3.8).

    À poser AU-DESSUS de ``@login_required`` (il s'exécute donc AVANT) : si un
    jeton valide est présent, l'utilisateur propriétaire est connecté pour CETTE
    requête seulement — ``login_user(remember=False)`` puis ``session.modified =
    False`` : aucun cookie de session n'est émis, chaque appel réauthentifie.
    Un Bearer `tia_` invalide est un 401 JSON sec (pas de repli silencieux sur
    le cookie : un script au jeton révoqué doit le savoir). Sans en-tête, le
    chemin session/cookie historique s'applique tel quel.
    """
    import functools

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            raw = auth_header[len("Bearer "):].strip()
            from transcria.auth.api_tokens import TOKEN_PREFIX, authenticate_token

            if raw.startswith(TOKEN_PREFIX):
                user = authenticate_token(raw)
                if user is None:
                    return jsonify({"error": "Jeton d'API invalide, expiré ou révoqué"}), 401
                from flask import session
                from flask_login import login_user

                login_user(user, remember=False)
                session.permanent = False
                session.modified = False        # pas de Set-Cookie : jeton ≠ session
        return view(*args, **kwargs)

    return wrapper

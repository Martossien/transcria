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
                    # Traçabilité : une tentative avec un jeton invalide/expiré/révoqué
                    # est un signal de sécurité (jeton volé, sondage). Un usage RÉUSSI
                    # n'est PAS audité par requête (le polling inonderait) : les actions
                    # en aval portent déjà l'utilisateur, et token.last_used_at trace l'usage.
                    _audit_invalid_api_token(raw)
                    return jsonify({"error": "Jeton d'API invalide, expiré ou révoqué"}), 401
                from flask import session
                from flask_login import login_user

                login_user(user, remember=False)
                session.permanent = False
                session.modified = False        # pas de Set-Cookie : jeton ≠ session
        return view(*args, **kwargs)

    return wrapper


def _audit_invalid_api_token(raw: str) -> None:
    """Trace une tentative avec un jeton `tia_` invalide/expiré/révoqué (signal de
    sécurité). On journalise le token_id (partie PUBLIQUE) — jamais le secret."""
    from transcria.audit.decorator import audit_log
    from transcria.audit.models import AuditAction
    from transcria.auth.api_tokens import parse_token

    parsed = parse_token(raw)
    audit_log(AuditAction.LOGIN_FAILED, target_label="api_token",
              details={"reason": "api_token_invalid", "token_id": parsed[0] if parsed else None})


def bearer_token_required(view):
    """Exige un jeton d'API personnel valide — 401 JSON sinon (API machine-to-machine).

    Contrairement à ``bearer_token_allowed`` (repli silencieux sur le cookie de
    session), l'absence OU l'invalidité du jeton est ici un 401 JSON sec. Adapté à
    la façade ``/v1`` : hors préfixe ``/api``, le handler d'auth global ne renvoie
    pas le 401 JSON (il redirigerait) — on l'impose donc ici. En cas de succès,
    l'utilisateur propriétaire est connecté pour CETTE requête seulement (aucun
    cookie émis) : ``@requires``/vérifications de permission voient alors le user.
    """
    import functools

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        raw = auth_header[len("Bearer "):].strip() if auth_header.startswith("Bearer ") else ""
        from transcria.auth.api_tokens import TOKEN_PREFIX, authenticate_token

        if not raw.startswith(TOKEN_PREFIX):
            return jsonify({"error": "Jeton d'API requis (Authorization: Bearer tia_…)"}), 401
        user = authenticate_token(raw)
        if user is None:
            _audit_invalid_api_token(raw)
            return jsonify({"error": "Jeton d'API invalide, expiré ou révoqué"}), 401
        from flask import session
        from flask_login import login_user

        login_user(user, remember=False)
        session.permanent = False
        session.modified = False        # pas de Set-Cookie : jeton ≠ session
        return view(*args, **kwargs)

    return wrapper

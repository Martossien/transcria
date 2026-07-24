"""Content-Security-Policy (chantier sécurité, opt-in).

Depuis la migration des gestionnaires inline vers une délégation (``ui_actions.js``),
``script-src`` peut être STRICT : ``'self'`` + un **nonce par requête** pour les rares
îlots de données inline (``<script nonce=…>window.X = …|tojson</script>``), SANS
``'unsafe-inline'``. Toute injection de script non prévue est alors bloquée.

Trois modes (``security.csp``) :
- ``off`` (défaut) : aucun en-tête ;
- ``report-only`` : ``Content-Security-Policy-Report-Only`` — le navigateur SIGNALE
  les violations (console) sans rien bloquer → déploiement sûr, observation d'abord ;
- ``enforce`` : ``Content-Security-Policy`` — appliqué.

``style-src`` garde ``'unsafe-inline'`` (Bootstrap pose des styles inline via JS ;
l'injection de STYLE est un risque bien moindre que celle de script).
"""
from __future__ import annotations

import secrets

CSP_MODES = ("off", "report-only", "enforce")

_CDN = "https://cdn.jsdelivr.net"


def get_request_nonce() -> str:
    """Nonce CSP de la requête (créé une fois, partagé entre les balises <script>
    des templates et l'en-tête). Exposé aux templates via ``csp_nonce()``."""
    from flask import g

    n = getattr(g, "_csp_nonce", None)
    if n is None:
        n = secrets.token_urlsafe(16)
        g._csp_nonce = n
    return n


def build_policy(nonce: str | None) -> str:
    # script-src STRICT dès qu'un nonce est disponible (handlers inline migrés) ;
    # repli 'unsafe-inline' seulement si aucun nonce (ne devrait pas arriver en requête).
    script_src = ["'self'", _CDN, f"'nonce-{nonce}'"] if nonce else ["'self'", _CDN, "'unsafe-inline'"]
    directives = (
        ("default-src", ("'self'",)),
        ("script-src", tuple(script_src)),
        ("style-src", ("'self'", "'unsafe-inline'", _CDN)),   # Bootstrap : styles inline
        ("img-src", ("'self'", "data:")),
        ("font-src", ("'self'", _CDN)),
        # blob: pour l'aperçu de l'enregistrement micro (URL.createObjectURL) — sans lui,
        # <audio src="blob:…"> serait bloqué sous CSP enforce (media-src retombe sur default-src).
        ("media-src", ("'self'", "blob:")),
        ("connect-src", ("'self'",)),
        ("object-src", ("'none'",)),
        ("base-uri", ("'self'",)),
        ("frame-ancestors", ("'none'",)),
        ("form-action", ("'self'",)),
    )
    return "; ".join(f"{name} {' '.join(src)}" for name, src in directives)


def csp_header(mode: str, nonce: str | None = None) -> tuple[str, str] | None:
    """(nom d'en-tête, valeur) pour le mode donné, ou None si ``off``/inconnu."""
    mode = str(mode or "off").strip().lower()
    if mode == "enforce":
        return ("Content-Security-Policy", build_policy(nonce))
    if mode == "report-only":
        return ("Content-Security-Policy-Report-Only", build_policy(nonce))
    return None

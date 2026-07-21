"""Content-Security-Policy (chantier sécurité, opt-in).

Politique construite pour être SÛRE à déployer sans casser l'interface : forte
sur tous les vecteurs sauf le script inline, qui reste autorisé (`'unsafe-inline'`)
tant que les gestionnaires d'événements inline (`onclick=`, ~59 occurrences) et les
îlots de données `<script>window.X = …|tojson</script>` vivent dans les templates.

Trois modes (`security.csp`) :
- ``off`` (défaut) : aucun en-tête ;
- ``report-only`` : ``Content-Security-Policy-Report-Only`` — le navigateur SIGNALE
  les violations (console) sans rien bloquer → déploiement sûr, observation d'abord ;
- ``enforce`` : ``Content-Security-Policy`` — appliqué.

Étape suivante (script-src STRICT, sans ``'unsafe-inline'``, à base de nonces) :
migrer les gestionnaires inline vers des écouteurs délégués + nonce sur les îlots
de données — nécessite une validation navigateur de chaque interaction.
"""
from __future__ import annotations

CSP_MODES = ("off", "report-only", "enforce")

# `unsafe-inline` sur script/style = INTÉRIMAIRE (handlers + styles inline). Le reste
# est verrouillé : pas d'objets/embeds, pas de <base> injecté, jamais en iframe, les
# formulaires ne peuvent poster qu'en local, ressources/connexions same-origin (+ CDN
# Bootstrap explicitement listé, seule origine tierce).
_CDN = "https://cdn.jsdelivr.net"
_DIRECTIVES = (
    ("default-src", ("'self'",)),
    ("script-src", ("'self'", "'unsafe-inline'", _CDN)),
    ("style-src", ("'self'", "'unsafe-inline'", _CDN)),
    ("img-src", ("'self'", "data:")),
    ("font-src", ("'self'", _CDN)),
    ("connect-src", ("'self'",)),
    ("object-src", ("'none'",)),
    ("base-uri", ("'self'",)),
    ("frame-ancestors", ("'none'",)),
    ("form-action", ("'self'",)),
)

_POLICY = "; ".join(f"{name} {' '.join(sources)}" for name, sources in _DIRECTIVES)


def csp_header(mode: str) -> tuple[str, str] | None:
    """(nom d'en-tête, valeur) pour le mode donné, ou None si ``off``/inconnu."""
    mode = str(mode or "off").strip().lower()
    if mode == "enforce":
        return ("Content-Security-Policy", _POLICY)
    if mode == "report-only":
        return ("Content-Security-Policy-Report-Only", _POLICY)
    return None

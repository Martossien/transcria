"""i18n des sorties CLI hors-web (installateur, doctor) — FR/EN, sans dépendance.

L'interface web utilise Flask-Babel/gettext (``transcria/web/i18n.py``). Mais l'installateur
(``install.sh`` + ``transcria/install_*``/``transcria/installer/*``) et le diagnostic
(``transcria/diagnostics/doctor.py``) tournent AVANT/HORS du contexte Flask, et souvent avant
que les ``.mo`` soient compilés. On leur donne donc un i18n autonome : de simples tables
``{"fr": {...}, "en": {...}}`` et un traducteur qui résout la locale depuis
``TRANSCRIA_DEFAULT_LOCALE`` (exporté par ``install.sh``, aligné sur ``i18n.default_locale``).

Contrat de non-régression : locale par défaut = ``fr`` ⇒ chaîne rendue IDENTIQUE à l'historique
(les catalogues ``fr`` reprennent les libellés existants mot pour mot). Une clé absente d'``en``
retombe sur ``fr`` ; une clé absente partout retombe sur la clé brute (jamais de crash).
"""
from __future__ import annotations

import os
from collections.abc import Callable, Mapping

_AVAILABLE = ("fr", "en")
_DEFAULT = "fr"

# Type d'un catalogue : {locale: {clé: gabarit}}. Les gabarits utilisent ``str.format`` (``{x}``).
Catalog = dict[str, dict[str, str]]


def resolve_cli_locale(env: Mapping[str, str] | None = None) -> str:
    """Locale des sorties CLI : ``TRANSCRIA_DEFAULT_LOCALE`` filtré par l'allowlist, sinon ``fr``.

    ``install.sh`` exporte cette variable (choix de langue en tête d'install) ; elle est aussi
    l'override de ``i18n.default_locale`` côté application (cf. ``config/loader``), donc la
    langue reste cohérente entre l'install, le doctor et l'interface web.
    """
    env = env if env is not None else os.environ
    loc = env.get("TRANSCRIA_DEFAULT_LOCALE") or ""
    return loc if loc in _AVAILABLE else _DEFAULT


def make_translator(catalog: Catalog, *, locale: str | None = None) -> Callable[..., str]:
    """Retourne ``t(key, **kw)`` lié à ``catalog`` et à la locale résolue (repli fr → clé brute).

    ``locale`` explicite (tests) sinon résolue depuis l'environnement. ``t`` applique
    ``str.format(**kw)`` uniquement si des variables sont fournies (sûr pour les libellés
    contenant des accolades littérales sans placeholder).
    """
    loc = locale or resolve_cli_locale()
    fr_table = catalog.get(_DEFAULT, {})
    table = catalog.get(loc, fr_table)

    def t(key: str, /, **kw: object) -> str:
        template = table.get(key) or fr_table.get(key) or key
        return template.format(**kw) if kw else template

    return t

"""Branchement Flask-Babel de l'app web (la résolution de locale vit dans transcria.i18n).

Vague A1 (2026-07-13) : la mécanique de sélection de locale et le catalogue JS sont
descendus dans ``transcria/i18n/`` (couche noyau) — l'interface ne doit être une
dépendance de personne. Ici ne reste que ce qui est réellement lié à l'app Flask :
``init_app``. Les ré-exports ci-dessous sont un SHIM de dépréciation (une release).
"""
from __future__ import annotations

import logging

from flask import Flask, Response, current_app

from transcria.i18n.locale import (  # noqa: F401 — shim de dépréciation (vague A1)
    SESSION_LOCALE_KEY,
    available_locales,
    capture_lang_override,
    default_locale,
    select_locale,
)

logger = logging.getLogger(__name__)


def init_app(app: Flask) -> None:
    """Branche Flask-Babel sur l'app : sélecteur, globals Jinja, route de catalogue JS.

    Import local de flask_babel pour que ce module reste importable même si la dépendance
    n'est pas installée (ex. outillage hors-web) — l'appel réel exige la dépendance.
    """
    import os

    from flask_babel import Babel, get_locale

    # Les catalogues vivent dans transcria/web/translations/ (à côté de ce module), pas dans
    # le <root_path>/translations par défaut de Flask-Babel. Chemin ABSOLU → résolu pareil
    # quel que soit le CWD / HOME (service en root inclus, cf. runtime_root_vs_admin_env).
    translations_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "translations")
    app.config.setdefault("BABEL_TRANSLATION_DIRECTORIES", translations_dir)
    app.config.setdefault("BABEL_DEFAULT_LOCALE", default_locale())

    Babel(app, locale_selector=select_locale)

    # `get_locale` disponible dans les templates (ex. <html lang="{{ get_locale() }}">).
    app.jinja_env.globals["get_locale"] = get_locale
    app.jinja_env.globals["available_locales"] = available_locales

    @app.before_request
    def _capture_lang() -> None:
        capture_lang_override()

    @app.route("/i18n/messages.js")
    def i18n_messages_js() -> Response:
        """Catalogue JS : ``window.I18N`` = { source_fr: traduction } pour la locale courante.

        Source unique de vérité = les mêmes catalogues gettext. Le helper ``t()`` (i18n.js)
        cherche par la chaîne source française (convention gettext : msgid = source)."""
        from transcria.i18n.js_catalog import build_js_catalog

        body = build_js_catalog(str(get_locale()))
        resp = current_app.response_class(body, mimetype="application/javascript")
        # Le cache-busting est assuré par ?v=… posé dans le template (asset_url-like) ; on
        # autorise un cache court côté navigateur.
        resp.headers["Cache-Control"] = "public, max-age=300"
        return resp

    logger.debug("Flask-Babel initialisé (locales=%s, défaut=%s)", available_locales(), default_locale())

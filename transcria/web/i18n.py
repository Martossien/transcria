"""Internationalisation de l'interface (axe A) : intégration Flask-Babel.

Ce module centralise **la résolution de la locale** de l'interface et **le catalogue JS**
(les chaînes traduites côté navigateur). Il ne contient aucune chaîne traduisible — juste la
mécanique. Les catalogues gettext vivent dans ``transcria/web/translations/<code>/``.

Rappel de conception (cf. docs/I18N_MULTILANGUE.md) : la locale de l'INTERFACE (ici) est
distincte de la langue des LIVRABLES générés, qui est un réglage par job. On ne les couple pas.
"""
from __future__ import annotations

import logging

from flask import Flask, Response, current_app, request, session
from flask_login import current_user

logger = logging.getLogger(__name__)

# Clé de session où l'on mémorise un override explicite (?lang=xx).
SESSION_LOCALE_KEY = "ui_locale"


def available_locales(cfg: dict | None = None) -> list[str]:
    """Allowlist des locales proposées (config ``i18n.available_locales``)."""
    if cfg is None:
        from transcria.config import get_config

        cfg = get_config()
    locales = (cfg.get("i18n", {}) or {}).get("available_locales") or ["fr"]
    # Défensif : garantir des str non vides et l'unicité en préservant l'ordre.
    seen: dict[str, None] = {}
    for code in locales:
        if isinstance(code, str) and code:
            seen.setdefault(code, None)
    return list(seen) or ["fr"]


def default_locale(cfg: dict | None = None) -> str:
    """Locale par défaut de l'instance (config ``i18n.default_locale``)."""
    if cfg is None:
        from transcria.config import get_config

        cfg = get_config()
    default = (cfg.get("i18n", {}) or {}).get("default_locale") or "fr"
    allowed = available_locales(cfg)
    return default if default in allowed else allowed[0]


def select_locale() -> str:
    """Sélecteur de locale passé à Flask-Babel (appelé à chaque requête).

    Ordre de priorité (le plus spécifique gagne) :
      1. override explicite ``?lang=xx`` mémorisé en session ;
      2. préférence de l'utilisateur connecté (``current_user.locale``) ;
      3. meilleure correspondance de l'en-tête ``Accept-Language`` du navigateur ;
      4. locale par défaut de l'instance (config).
    Toujours filtré par l'allowlist ``i18n.available_locales`` : une valeur hors liste
    retombe sur le défaut.
    """
    allowed = available_locales()

    # 1. Override de session (posé par capture_lang_override sur ?lang=).
    chosen = session.get(SESSION_LOCALE_KEY)
    if chosen in allowed:
        return chosen

    # 2. Préférence utilisateur persistée.
    try:
        if current_user and current_user.is_authenticated:
            pref = getattr(current_user, "locale", None)
            if pref in allowed:
                return pref
    except Exception:  # noqa: BLE001 — hors contexte de requête / user proxy indisponible
        pass

    # 3. Négociation navigateur.
    try:
        best = request.accept_languages.best_match(allowed)
        if best:
            return best
    except Exception:  # noqa: BLE001 — pas de requête active (tâche de fond, tests)
        pass

    # 4. Défaut instance.
    return default_locale()


def capture_lang_override() -> None:
    """``before_request`` : applique ``?lang=xx`` (session + préférence utilisateur persistée).

    On valide contre l'allowlist pour ne jamais stocker une locale non gérée. Pour un
    utilisateur connecté, on persiste aussi le choix dans ``user.locale`` → il le retrouve
    d'un appareil à l'autre, sans formulaire dédié (le sélecteur navbar suffit).
    """
    lang = request.args.get("lang")
    if not lang or lang not in available_locales():
        return  # absent ou hors allowlist → ignoré silencieusement (pas d'erreur bloquante)
    session[SESSION_LOCALE_KEY] = lang
    try:
        if current_user and current_user.is_authenticated and getattr(current_user, "locale", None) != lang:
            from transcria.auth.store import UserStore

            UserStore.update_user(current_user.get_id(), locale=lang)
    except Exception:  # noqa: BLE001 — la persistance de préférence ne doit jamais casser une requête
        logger.debug("Persistance de la locale utilisateur impossible", exc_info=True)


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
        from transcria.web.i18n_js import build_js_catalog

        body = build_js_catalog(str(get_locale()))
        resp = current_app.response_class(body, mimetype="application/javascript")
        # Le cache-busting est assuré par ?v=… posé dans le template (asset_url-like) ; on
        # autorise un cache court côté navigateur.
        resp.headers["Cache-Control"] = "public, max-age=300"
        return resp

    logger.debug("Flask-Babel initialisé (locales=%s, défaut=%s)", available_locales(), default_locale())

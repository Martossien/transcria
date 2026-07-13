"""Internationalisation transverse (couche noyau — cf. docs/REFACTORING_QUALITE.md, vague A1).

Résolution de la locale d'interface et catalogue des chaînes JS. Vivait dans
``transcria/web/`` ; extrait ici parce que les blueprints hors web (contexte, voix, file)
en dépendent — l'interface ne doit être une dépendance de personne. Le branchement
Flask-Babel de l'app (``init_app``) reste côté web (``transcria/web/i18n.py``).
"""
from transcria.i18n.js_catalog import JS_MESSAGES, N_, build_js_catalog
from transcria.i18n.locale import (
    SESSION_LOCALE_KEY,
    available_locales,
    capture_lang_override,
    default_locale,
    select_locale,
)

__all__ = [
    "JS_MESSAGES",
    "N_",
    "SESSION_LOCALE_KEY",
    "available_locales",
    "build_js_catalog",
    "capture_lang_override",
    "default_locale",
    "select_locale",
]

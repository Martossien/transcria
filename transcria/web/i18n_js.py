"""Catalogue des chaînes traduites exposées au JavaScript (axe A, Option 1).

Le front appelle ``t("chaîne source française")`` (helper static/js/i18n.js). Ici on liste
les **chaînes source** (msgid = français, convention gettext) utilisées côté navigateur, et on
construit ``window.I18N = { source: traduction }`` pour la locale courante.

Source UNIQUE de vérité = les mêmes catalogues gettext que le reste de l'UI : chaque entrée de
``JS_MESSAGES`` doit apparaître aussi dans ``messages.po`` (extraction pybabel via l'appel
``gettext`` ci-dessous, que babel.cfg récolte comme les .py). Les vagues suivantes ajoutent
leurs chaînes JS à ``JS_MESSAGES``.
"""
from __future__ import annotations

import json

from flask_babel import gettext

# Chaînes utilisées dans le JavaScript (français source). Tenu à jour à la main, vague après
# vague. Vide au socle (la Vague 0 ne touche pas au JS applicatif) ; la Vague 1 le peuple.
JS_MESSAGES: tuple[str, ...] = ()


def build_js_catalog(locale: str) -> str:
    """Rend le corps JS ``window.I18N = {…}; window.I18N_LOCALE = "xx";``.

    ``locale`` sert au marqueur exposé au front (et au débogage) ; la traduction elle-même
    utilise la locale déjà résolue pour la requête par Flask-Babel.
    """
    catalog = {source: gettext(source) for source in JS_MESSAGES}
    return (
        "window.I18N = " + json.dumps(catalog, ensure_ascii=False) + ";\n"
        "window.I18N_LOCALE = " + json.dumps(locale) + ";\n"
    )

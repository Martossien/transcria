"""Résolution de la locale de l'interface (extrait de transcria/web/i18n.py — vague A1).

Aucune chaîne traduisible ici — juste la mécanique de sélection. Les catalogues gettext
restent dans ``transcria/web/translations/<code>/`` (le branchement Flask-Babel de l'app,
``init_app``, vit toujours côté web).

Rappel de conception (cf. docs/I18N_MULTILANGUE.md) : la locale de l'INTERFACE (ici) est
distincte de la langue des LIVRABLES générés, qui est un réglage par job. On ne les couple pas.
"""
from __future__ import annotations

import logging

from flask import request, session
from flask_login import current_user

from transcria.auth.store import UserStore
from transcria.config import get_config

logger = logging.getLogger(__name__)

# Clé de session où l'on mémorise un override explicite (?lang=xx).
SESSION_LOCALE_KEY = "ui_locale"


def available_locales(cfg: dict | None = None) -> list[str]:
    """Allowlist des locales proposées (config ``i18n.available_locales``)."""
    if cfg is None:
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
            UserStore.update_user(current_user.get_id(), locale=lang)
    except Exception:  # noqa: BLE001 — la persistance de préférence ne doit jamais casser une requête
        logger.debug("Persistance de la locale utilisateur impossible", exc_info=True)

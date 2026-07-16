"""Socle i18n (Vague 0) : résolution de locale, catalogue JS, config, phase de compilation.

On teste le CŒUR (fonctions pures + intégration légère via l'app de test). Le rendu réel des
templates traduits est couvert par les tests de routes existants (l'app monte Flask-Babel).
"""
from __future__ import annotations

from pathlib import Path

from transcria.config import config_schema
from transcria.web import i18n as web_i18n

# --- Config : available_locales / default_locale -----------------------------------------

def test_available_locales_from_config():
    cfg = {"i18n": {"available_locales": ["fr", "en", "es"]}}
    assert web_i18n.available_locales(cfg) == ["fr", "en", "es"]


def test_available_locales_dedup_and_fallback():
    assert web_i18n.available_locales({"i18n": {"available_locales": ["fr", "fr", "", 3]}}) == ["fr"]
    assert web_i18n.available_locales({}) == ["fr"]


def test_default_locale_valid_and_coerced():
    assert web_i18n.default_locale({"i18n": {"default_locale": "en", "available_locales": ["fr", "en"]}}) == "en"
    # défaut hors allowlist → 1re locale disponible
    assert web_i18n.default_locale({"i18n": {"default_locale": "de", "available_locales": ["fr", "en"]}}) == "fr"


# --- Validation de schéma -----------------------------------------------------------------

def test_schema_accepts_valid_i18n():
    r = config_schema.ValidationResult()
    config_schema._check_i18n({"default_locale": "fr", "available_locales": ["fr", "en"]}, r)
    assert r.is_valid


def test_schema_rejects_default_not_in_available():
    r = config_schema.ValidationResult()
    config_schema._check_i18n({"default_locale": "en", "available_locales": ["fr"]}, r)
    assert not r.is_valid


def test_schema_warns_unknown_locale():
    r = config_schema.ValidationResult()
    config_schema._check_i18n({"available_locales": ["fr", "xx"]}, r)
    assert r.is_valid and any("xx" in w for w in r.warnings)


def test_schema_rejects_non_list_available():
    r = config_schema.ValidationResult()
    config_schema._check_i18n({"available_locales": "fr"}, r)
    assert not r.is_valid


# --- Sélecteur de locale (priorité) -------------------------------------------------------

def test_select_locale_priority_session_over_all(app):
    with app.test_request_context("/", headers={"Accept-Language": "en"}):
        from flask import session
        session[web_i18n.SESSION_LOCALE_KEY] = "en"
        assert web_i18n.select_locale() == "en"


def test_select_locale_accept_language(app):
    with app.test_request_context("/", headers={"Accept-Language": "en-US,en;q=0.9"}):
        assert web_i18n.select_locale() == "en"


def test_select_locale_falls_back_to_default(app):
    with app.test_request_context("/", headers={"Accept-Language": "zz"}):
        assert web_i18n.select_locale() == web_i18n.default_locale()


def test_capture_lang_override_stores_session(app):
    with app.test_request_context("/?lang=en"):
        from flask import session
        web_i18n.capture_lang_override()
        assert session.get(web_i18n.SESSION_LOCALE_KEY) == "en"


def test_capture_lang_override_ignores_unknown(app):
    with app.test_request_context("/?lang=zz"):
        from flask import session
        web_i18n.capture_lang_override()
        assert web_i18n.SESSION_LOCALE_KEY not in session


# --- Traductions réellement chargées ------------------------------------------------------

def test_gettext_english_translation(app):
    from flask_babel import force_locale, gettext, pgettext
    with app.test_request_context("/"):
        with force_locale("en"):
            assert gettext("Traitements") == "Jobs"
            assert pgettext("navigation", "File") == "Queue"
        with force_locale("fr"):
            assert gettext("Traitements") == "Traitements"


# --- Route de catalogue JS ----------------------------------------------------------------

def test_js_catalog_route(app):
    c = app.test_client()
    resp = c.get("/i18n/messages.js?lang=en")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["Content-Type"]
    body = resp.get_data(as_text=True)
    assert "window.I18N" in body and "window.I18N_LOCALE" in body


def test_html_lang_reflects_locale(app):
    # Page publique /login : override de SESSION uniquement (pas d'utilisateur connecté →
    # aucune écriture de user.locale, donc pas de pollution de l'admin partagé de la session).
    c = app.test_client()
    assert '<html lang="en">' in c.get("/login?lang=en").get_data(as_text=True)
    assert '<html lang="fr">' in c.get("/login?lang=fr").get_data(as_text=True)


# --- Phase de compilation (installer / entrypoint) ----------------------------------------

def test_i18n_phase_compiles(tmp_path: Path):
    from babel.messages.catalog import Catalog
    from babel.messages.pofile import write_po

    from transcria.installer.i18n_phase import I18nPlan, apply_i18n

    po_dir = tmp_path / "en" / "LC_MESSAGES"
    po_dir.mkdir(parents=True)
    catalog = Catalog(locale="en")
    catalog.add("Bonjour", "Hello")
    with (po_dir / "messages.po").open("wb") as fh:
        write_po(fh, catalog)

    class _C:
        def __init__(self): self.msgs = []
        def info(self, m): self.msgs.append(m)
        def ok(self, m): self.msgs.append(m)
        def warn(self, m): self.msgs.append(m)
        def error(self, m): self.msgs.append(m)

    console = _C()
    result = apply_i18n(I18nPlan(translations_dir=tmp_path), console=console)
    assert "en" in result.compiled
    assert (po_dir / "messages.mo").is_file()

    # Idempotent : 2e passe sans force → sauté.
    result2 = apply_i18n(I18nPlan(translations_dir=tmp_path), console=console)
    assert result2.skipped == ["en"] and result2.compiled == []


def test_i18n_phase_missing_dir_is_not_error(tmp_path: Path):
    from transcria.installer.i18n_phase import I18nPlan, apply_i18n

    class _C:
        def info(self, m): ...
        def ok(self, m): ...
        def warn(self, m): ...
        def error(self, m): ...

    result = apply_i18n(I18nPlan(translations_dir=tmp_path / "nope"), console=_C())
    assert result.compiled == [] and result.skipped == []


# --- Modèle User : colonne locale ---------------------------------------------------------

def test_user_locale_column_and_to_dict(app):
    with app.app_context():
        import uuid

        from transcria.auth.models import Role
        from transcria.auth.store import UserStore

        u = UserStore.create_user(username=f"loc_{uuid.uuid4().hex[:6]}", password="pw", role=Role.OPERATOR)
        assert "locale" in u.to_dict()
        UserStore.update_user(u.id, locale="en")
        assert UserStore.get_by_id(u.id).locale == "en"

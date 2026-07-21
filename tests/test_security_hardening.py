"""Durcissement HTTP(S) (0.3.9.1) — cookie Secure, ProxyFix scheme-only, HSTS,
garde d'origine CSRF, check doctor. Tout est OPT-IN (défaut = comportement inchangé)."""
from __future__ import annotations

import os
from copy import deepcopy

from werkzeug.middleware.proxy_fix import ProxyFix

from transcria.config.loader import get_default_config
from transcria.diagnostics import doctor as doc


def _app(_pg_database, **security):
    os.environ["TRANSCRIA_DATABASE_URL"] = _pg_database
    cfg = deepcopy(get_default_config())
    cfg["auth"]["first_admin_password"] = "admin-change-me"   # même graine que conftest
    cfg["security"].update(security)
    cfg.setdefault("storage", {})["jobs_dir"] = "/tmp/transcria-sec-tests"
    from app import create_app
    app = create_app(config=cfg, start_background_services=False)
    app.config["TESTING"] = True
    return app


class TestCookieEtProxy:
    def test_defaut_inchange(self, _pg_database):
        app = _app(_pg_database)
        assert app.config["SESSION_COOKIE_SECURE"] is False
        assert not isinstance(app.wsgi_app, ProxyFix)

    def test_behind_tls_proxy_scheme_seul_et_cookie_secure(self, _pg_database):
        app = _app(_pg_database, behind_tls_proxy=True)
        assert app.config["SESSION_COOKIE_SECURE"] is True
        assert isinstance(app.wsgi_app, ProxyFix)
        # SÉCURITÉ CLÉ : jamais x_for (sinon remote_addr = X-Forwarded-For, anti-bruteforce contourné).
        assert app.wsgi_app.x_for == 0
        assert app.wsgi_app.x_proto == 1

    def test_cookie_secure_explicite_sans_proxy(self, _pg_database):
        app = _app(_pg_database, session_cookie_secure=True)
        assert app.config["SESSION_COOKIE_SECURE"] is True
        assert not isinstance(app.wsgi_app, ProxyFix)


class TestHSTS:
    def test_emis_uniquement_sur_https_reel(self, _pg_database):
        app = _app(_pg_database, behind_tls_proxy=True, hsts_enabled=True)
        client = app.test_client()
        r_http = client.get("/login")
        r_https = client.get("/login", headers={"X-Forwarded-Proto": "https"})
        assert "Strict-Transport-Security" not in r_http.headers      # jamais sur HTTP en clair
        assert "max-age=31536000; includeSubDomains" == r_https.headers.get("Strict-Transport-Security")

    def test_max_age_configurable(self, _pg_database):
        app = _app(_pg_database, behind_tls_proxy=True, hsts_enabled=True, hsts_max_age_days=30)
        r = app.test_client().get("/login", headers={"X-Forwarded-Proto": "https"})
        assert f"max-age={30 * 86400}" in r.headers.get("Strict-Transport-Security")

    def test_desactive_par_defaut(self, _pg_database):
        app = _app(_pg_database, behind_tls_proxy=True)   # hsts_enabled False
        r = app.test_client().get("/login", headers={"X-Forwarded-Proto": "https"})
        assert "Strict-Transport-Security" not in r.headers


class TestGardeOrigine:
    def test_post_origine_croisee_refuse(self, _pg_database):
        app = _app(_pg_database, csrf_origin_check=True)
        r = app.test_client().post("/login", data={"username": "x", "password": "y"},
                                   headers={"Origin": "https://evil.example"})
        assert r.status_code == 403

    def test_post_meme_origine_passe(self, _pg_database):
        app = _app(_pg_database, csrf_origin_check=True)
        r = app.test_client().post("/login", data={"username": "x", "password": "y"},
                                   headers={"Origin": "http://localhost"})
        assert r.status_code != 403   # 401 (identifiants), pas bloqué par la garde

    def test_origin_absent_non_bloque(self, _pg_database):
        app = _app(_pg_database, csrf_origin_check=True)
        r = app.test_client().post("/login", data={"username": "x", "password": "y"})
        assert r.status_code != 403   # SameSite reste la garde

    def test_jeton_bearer_exempt(self, _pg_database):
        app = _app(_pg_database, csrf_origin_check=True)
        r = app.test_client().post("/api/jobs/x/process",
                                   headers={"Origin": "https://evil.example", "Authorization": "Bearer tia_a_b"})
        assert r.status_code != 403   # l'API par jeton n'a pas de navigateur → exemptée

    def test_get_non_affecte(self, _pg_database):
        app = _app(_pg_database, csrf_origin_check=True)
        r = app.test_client().get("/login", headers={"Origin": "https://evil.example"})
        assert r.status_code == 200

    def test_desactive_pas_deffet(self, _pg_database):
        app = _app(_pg_database)   # csrf_origin_check False
        r = app.test_client().post("/login", data={"username": "x", "password": "y"},
                                   headers={"Origin": "https://evil.example"})
        assert r.status_code != 403


class TestSchemaEtFormulaire:
    def test_hsts_sans_proxy_avertit(self):
        from transcria.config.config_schema import validate_config
        cfg = deepcopy(get_default_config())
        cfg["security"]["hsts_enabled"] = True
        result = validate_config(cfg)
        assert not result.errors
        assert any("HSTS n'est émis" in w for w in result.warnings)

    def test_champs_dans_le_formulaire_admin(self):
        from transcria.web.config_form import CONFIG_FORM_SECTIONS, iter_fields
        paths = {f["path"] for f in iter_fields(CONFIG_FORM_SECTIONS)}
        for p in ("security.behind_tls_proxy", "security.session_cookie_secure",
                  "security.hsts_enabled", "security.csrf_origin_check"):
            assert p in paths, p


class TestDoctorTransport:
    def test_federe_sans_transport_securise_warn(self):
        res = doc.check_transport_security({"auth": {"backend": "ldap"}, "security": {}})
        assert res.status == doc.WARN
        assert "ldap" in res.detail

    def test_federe_avec_proxy_tls_ok(self):
        res = doc.check_transport_security(
            {"auth": {"backend": "oidc"}, "security": {"behind_tls_proxy": True}})
        assert res.status == doc.OK

    def test_local_http_ok(self):
        res = doc.check_transport_security({"auth": {"backend": "local"}, "security": {}})
        assert res.status == doc.OK

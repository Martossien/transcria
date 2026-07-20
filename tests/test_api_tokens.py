"""Chantier identité lot 4 — jetons d'API personnels (tia_) sur les routes ⭐.

Le fil sécurité : secret jamais stocké (SHA-256 seul), comparaison en temps
constant, révocation/expiration/compte-désactivé refusés, périmètre STRICT aux
routes ⭐, aucun cookie émis (un jeton n'ouvre pas de session), et le jeton
porte les permissions de son propriétaire — jamais plus.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from transcria.auth.api_tokens import authenticate_token, create_token, parse_token
from transcria.auth.models import Role


class TestFormatEtParse:
    def test_parse_nominal_et_malformes(self):
        assert parse_token("tia_abc123_secretXYZ") == ("abc123", "secretXYZ")
        assert parse_token("tia_abc123_se_cret") == ("abc123", "se_cret")  # secret peut contenir _
        assert parse_token("") is None
        assert parse_token("Bearer tia_a_b") is None      # préfixe exact exigé
        assert parse_token("tia_sanssecret") is None
        assert parse_token("tia__secret") is None
        assert parse_token("jwt.eyJh.abc") is None

    def test_create_format_et_hachage_seul_en_base(self, app):
        with app.app_context():
            from transcria.auth.store import UserStore
            user = UserStore.create_user("tok-format", "x" * 24, role=Role.OPERATOR)
            full, token = create_token(user.id, "mon script")
            assert full.startswith("tia_") and full.split("_", 2)[1] == token.token_id
            secret = full.split("_", 2)[2]
            assert secret not in token.secret_hash        # jamais le secret en clair
            assert len(token.secret_hash) == 64           # sha256 hex
            assert token.label == "mon script"


@pytest.fixture()
def token_user(app):
    """Un opérateur + un jeton frais ; retourne (user_id, secret_complet)."""
    with app.app_context():
        import uuid

        from transcria.auth.store import UserStore
        user = UserStore.create_user(f"tok-{uuid.uuid4().hex[:8]}", "x" * 24, role=Role.OPERATOR)
        full, _tok = create_token(user.id, "test")
        return user.id, full


class TestAuthentification:
    def test_jeton_valide_authentifie(self, app, token_user):
        user_id, full = token_user
        with app.app_context():
            user = authenticate_token(full)
            assert user is not None and user.id == user_id

    def test_mauvais_secret_refuse(self, app, token_user):
        _, full = token_user
        with app.app_context():
            assert authenticate_token(full[:-4] + "AAAA") is None

    def test_revoque_refuse(self, app, token_user):
        user_id, full = token_user
        with app.app_context():
            from transcria.auth.api_tokens import list_for_user, revoke_token
            revoke_token(list_for_user(user_id)[0])
            assert authenticate_token(full) is None

    def test_expire_refuse(self, app):
        with app.app_context():
            from transcria.auth.store import UserStore
            user = UserStore.create_user("tok-expire", "x" * 24, role=Role.OPERATOR)
            full, _ = create_token(user.id, "vieux",
                                   expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
            assert authenticate_token(full) is None

    def test_compte_desactive_refuse(self, app, token_user):
        user_id, full = token_user
        with app.app_context():
            from transcria.auth.models import User, db
            db.session.get(User, user_id).is_active = False
            db.session.commit()
            assert authenticate_token(full) is None

    def test_last_used_throttle_une_ecriture_par_minute(self, app, token_user):
        user_id, full = token_user
        with app.app_context():
            from transcria.auth.api_tokens import list_for_user
            authenticate_token(full)
            first = list_for_user(user_id)[0].last_used_at
            assert first is not None
            authenticate_token(full)                       # < 1 min plus tard
            assert list_for_user(user_id)[0].last_used_at == first


class TestRoutesEtoile:
    def test_bearer_valide_sur_route_etoile_sans_cookie(self, app, token_user):
        _, full = token_user
        client = app.test_client()
        # Job inconnu → 404 JSON = l'AUTHENTIFICATION est passée (sinon redirection login).
        r = client.get("/api/jobs/inexistant/status", headers={"Authorization": f"Bearer {full}"})
        assert r.status_code == 404 and r.get_json()["error"] == "Job not found"
        assert "Set-Cookie" not in r.headers              # jeton ≠ session

    def test_sans_jeton_redirection_login_intacte(self, app):
        r = app.test_client().get("/api/jobs/inexistant/status")
        assert r.status_code in (302, 401)                # comportement historique

    def test_jeton_revoque_401_json_sec(self, app, token_user):
        user_id, full = token_user
        with app.app_context():
            from transcria.auth.api_tokens import list_for_user, revoke_token
            revoke_token(list_for_user(user_id)[0])
        r = app.test_client().get("/api/jobs/inexistant/status",
                                  headers={"Authorization": f"Bearer {full}"})
        assert r.status_code == 401
        assert "révoqué" in r.get_json()["error"]

    def test_bearer_non_tia_ignore_sans_500(self, app):
        r = app.test_client().get("/api/jobs/inexistant/status",
                                  headers={"Authorization": "Bearer eyJhbGciOi.autre.jwt"})
        assert r.status_code in (302, 401)                # chemin session historique

    def test_hors_perimetre_etoile_jamais_authentifie(self, app, token_user):
        """v1 : le Bearer ne vaut QUE sur les routes ⭐ — la page d'accueil
        redirige vers login même avec un jeton valide."""
        _, full = token_user
        r = app.test_client().get("/", headers={"Authorization": f"Bearer {full}"})
        assert r.status_code == 302 and "/login" in r.headers["Location"]

    def test_le_jeton_porte_les_permissions_du_proprietaire(self, app, admin_client, token_user):
        """Un jeton d'OPÉRATEUR ne peut pas toucher le job d'un AUTRE (403
        propriété) : le jeton ne donne jamais plus que son propriétaire."""
        import uuid
        with app.app_context():
            from transcria.auth.store import UserStore
            from transcria.jobs.store import JobStore
            admin = UserStore.get_by_username("admin")
            job_id = JobStore.create_job(admin.id, title=f"job-admin-{uuid.uuid4().hex[:6]}").id
        _, full = token_user
        r = app.test_client().post(f"/api/jobs/{job_id}/process",
                                   headers={"Authorization": f"Bearer {full}"})
        assert r.status_code == 403


class TestPageMonCompte:
    def test_cycle_complet_creer_puis_revoquer(self, admin_client):
        page = admin_client.get("/account/tokens")
        assert page.status_code == 200
        r = admin_client.post("/account/tokens", data={"label": "cycle-ui", "expires_days": "30"})
        body = r.get_data(as_text=True)
        assert "tia_" in body and "cycle-ui" in body      # secret affiché UNE fois
        token_id = body.split("tia_")[1].split("_")[0]
        r2 = admin_client.post(f"/account/tokens/{token_id}/revoke", follow_redirects=True)
        assert "révoqué" in r2.get_data(as_text=True)

    def test_duree_invalide_400(self, admin_client):
        r = admin_client.post("/account/tokens", data={"label": "x", "expires_days": "-3"})
        assert r.status_code == 400

    def test_revocation_du_jeton_d_un_autre_impossible(self, app, admin_client, token_user):
        user_id, _full = token_user
        with app.app_context():
            from transcria.auth.api_tokens import list_for_user
            other_token_id = list_for_user(user_id)[0].token_id
        r = admin_client.post(f"/account/tokens/{other_token_id}/revoke", follow_redirects=True)
        assert "introuvable" in r.get_data(as_text=True)
        with app.app_context():
            from transcria.auth.api_tokens import list_for_user
            assert list_for_user(user_id)[0].revoked_at is None   # intact

"""Kit « exécutant distant » (docs/RUNNER_DISTANT_KIT.md) — sans réseau ni root.

Ce que ces tests verrouillent : le script généré est COMPLET (jeton, URL portail, commit
épinglé, unité systemd, venv minimal pyyaml) et fail-loud ; un nom dangereux est refusé
AVANT de fabriquer quoi que ce soit ; la route est réservée à l'admin, exige la
fonctionnalité active, émet un jeton frais étiqueté et n'audite JAMAIS le jeton.
"""
from __future__ import annotations

import pytest

from transcria.ingestion.runner_kit import build_kit_script, valid_runner_name


class TestBuildKitScript:
    def test_script_complet_et_epingle(self):
        script = build_kit_script(portal_url="https://portail.exemple/", token="tia_abc_secret",
                                  runner_name="runner-site-b", pin_commit="deadbeef" * 5)
        assert script.startswith("#!/usr/bin/env bash")
        assert "set -euo pipefail" in script
        assert 'PORTAL_URL="https://portail.exemple"' in script     # slash final retiré
        assert 'TOKEN="tia_abc_secret"' in script
        assert f'PIN="{"deadbeef" * 5}"' in script
        assert "python -m connector_service.runner" in script       # l'unité lance le démon
        assert "pip\" install --quiet --upgrade pyyaml" in script   # venv MINIMAL
        assert "chmod 0600" in script                               # jeton protégé au repos
        assert "rm -- " in script                                   # consigne de suppression

    def test_sans_clone_git_branche_par_defaut_annoncee(self):
        script = build_kit_script(portal_url="http://p", token="tia_x",
                                  runner_name="r1", pin_commit="")
        assert 'PIN="main"' in script and "non épinglée" in script

    def test_nom_dangereux_refuse(self):
        for name in ("", "a b", "x/../y", "n;rm", "-débute-par-tiret", "x" * 65):
            assert not valid_runner_name(name)
            with pytest.raises(ValueError):
                build_kit_script(portal_url="http://p", token="tia_x", runner_name=name)
        assert valid_runner_name("meeting-runner-2") and valid_runner_name("site_B.01")


class TestKitRoute:
    @pytest.fixture(autouse=True)
    def _baseline_off(self, app):
        """Leçon 0.3.5 : la config de la MACHINE (réunions activées pour les gates réels)
        ne doit pas fuir dans la suite — OFF par défaut, les fixtures surchargent."""
        from transcria.config import get_config
        cfg = get_config()
        prev = cfg.pop("connectors", None)
        yield
        if prev is None:
            cfg.pop("connectors", None)
        else:
            cfg["connectors"] = prev

    @pytest.fixture
    def meetings_on(self, app):
        from transcria.config import get_config
        cfg = get_config()
        prev = cfg.get("connectors")
        cfg["connectors"] = {"meetings": {"enabled": True, "runner_usernames": ["svc-runner"]}}
        yield
        if prev is None:
            cfg.pop("connectors", None)
        else:
            cfg["connectors"] = prev

    @pytest.fixture
    def svc_account(self, app):
        from transcria.auth.models import Role
        from transcria.auth.store import UserStore
        with app.app_context():
            if UserStore.get_by_username("svc-runner") is None:
                UserStore.create_user("svc-runner", "x" * 24, role=Role.OPERATOR)

    def test_telechargement_jeton_frais_et_audit_sans_secret(self, app, admin_client,
                                                             meetings_on, svc_account):
        r = admin_client.post("/admin/connecteurs/runners/kit", data={
            "runner_name": "site-b", "portal_url": "https://portail.exemple"})
        assert r.status_code == 200
        assert "attachment; filename=transcria-runner-site-b.sh" in r.headers["Content-Disposition"]
        body = r.get_data(as_text=True)
        assert 'RUNNER_NAME="site-b"' in body and "tia_" in body
        with app.app_context():
            from transcria.audit.store import AuditStore
            from transcria.auth.models import ApiToken
            from transcria.auth.store import UserStore
            from transcria.database import db
            user = UserStore.get_by_username("svc-runner")
            labels = [t.label for t in db.session.execute(
                db.select(ApiToken).where(ApiToken.user_id == user.id)).scalars()]
            assert "runner distant site-b (kit)" in labels       # jeton frais, étiqueté
            entry = AuditStore.query(action="meeting_runner_kit", limit=1)[0]
            assert "tia_" not in str(entry.details_json or "")   # JAMAIS le jeton audité

    def test_fonctionnalite_off_refuse(self, admin_client, svc_account):
        r = admin_client.post("/admin/connecteurs/runners/kit", data={
            "runner_name": "site-b", "portal_url": "https://p"}, follow_redirects=False)
        assert r.status_code == 302                              # retour check-list + message

    def test_nom_ou_url_invalide_refuse(self, admin_client, meetings_on, svc_account):
        for data in ({"runner_name": "a b", "portal_url": "https://p"},
                     {"runner_name": "ok", "portal_url": "ftp://p"}):
            r = admin_client.post("/admin/connecteurs/runners/kit", data=data)
            assert r.status_code == 302

    def test_reserve_a_l_admin(self, viewer_client, meetings_on):
        r = viewer_client.post("/admin/connecteurs/runners/kit", data={
            "runner_name": "x", "portal_url": "https://p"})
        assert r.status_code in (302, 403)                       # jamais un kit

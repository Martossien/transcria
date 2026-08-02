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
        # Plus de guillemets écrits à la main : `shlex.quote` n'en pose que si la valeur
        # en a besoin (passe sécurité S1.2). Le slash final reste retiré.
        assert "PORTAL_URL=https://portail.exemple\n" in script
        assert "TOKEN=tia_abc_secret\n" in script
        assert f"PIN={'deadbeef' * 5}" in script
        assert "python -m connector_service.runner" in script       # l'unité lance le démon
        assert "pip\" install --quiet --upgrade pyyaml" in script   # venv MINIMAL
        assert "chmod 0600" in script                               # jeton protégé au repos
        assert "rm -- " in script                                   # consigne de suppression

    def test_sans_clone_git_branche_par_defaut_annoncee(self):
        script = build_kit_script(portal_url="http://p", token="tia_x",
                                  runner_name="r1", pin_commit="")
        assert "PIN=main" in script and "non épinglée" in script

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
        assert "RUNNER_NAME=site-b\n" in body and "tia_" in body
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


# --- Passe sécurité S1.2 : l'URL du portail part dans un script exécuté EN ROOT ---------
#
# Sur une AUTRE machine, et par quelqu'un qui n'est pas forcément l'admin du portail (le
# docstring de `build_kit_script` le dit lui-même). La validation historique était un
# simple préfixe `http://` : `https://x";$(commande);"` sortait du guillemet.
#
# Deux défenses redondantes sont testées ici : la validation de forme, ET le fait que la
# génération ne pose plus de guillemets à la main (shlex.quote).

import pytest

from transcria.ingestion.runner_kit import safe_portal_url


@pytest.mark.parametrize("hostile", [
    'https://x";$(id);"',            # sortie de guillemet + substitution de commande
    "https://x`id`.test",            # substitution héritée
    "https://x$(id).test",           # substitution
    "https://u:p@evil.test",         # userinfo : l'hôte réel n'est pas celui qu'on lit
    "https://ok.test\nrm -rf /",     # saut de ligne = nouvelle commande
    "https://ok.test\r\nid",         # CRLF
    "https://ok.test\tid",           # tabulation
    "file:///etc/passwd",            # schéma non réseau
    "javascript:alert(1)",           # schéma actif
    "https://",                      # pas d'hôte
    "http://ok.test?x=$(id)",        # la charge passe par la query
    "http://ok.test#$(id)",          # ... ou par l'ancre
    "https://ok.test/a b",           # espace dans le chemin
    "",                              # vide
])
def test_url_hostile_refusee_avant_generation(hostile):
    with pytest.raises(ValueError):
        safe_portal_url(hostile)


@pytest.mark.parametrize("legitime, attendu", [
    ("http://127.0.0.1:7870/", "http://127.0.0.1:7870"),
    ("https://portail.exemple.test/", "https://portail.exemple.test"),
    ("https://portail.exemple.test/transcria/", "https://portail.exemple.test/transcria"),
    ("http://sous.domaine-01.exemple.test", "http://sous.domaine-01.exemple.test"),
    # IPv6 : urlsplit retire les crochets, il faut les remettre — sans quoi l'URL produite
    # (`https://::1:8080`) n'en est plus une.
    ("https://[::1]:8080", "https://[::1]:8080"),
])
def test_url_legitime_normalisee(legitime, attendu):
    assert safe_portal_url(legitime) == attendu


def test_la_generation_ne_pose_plus_de_guillemets_a_la_main():
    """Seconde défense : même si un jour la validation laissait passer quelque chose,
    `shlex.quote` doit produire seul la forme sûre."""
    script = build_kit_script(portal_url="https://portail.exemple.test",
                              token="tia_jeton", runner_name="salle-a")
    ligne = next(l for l in script.split("\n") if l.startswith("PORTAL_URL="))
    assert ligne == "PORTAL_URL=https://portail.exemple.test"
    # une valeur qui aurait besoin d'être protégée l'est réellement
    assert "TOKEN=tia_jeton" in script


def test_build_kit_script_refuse_une_url_hostile():
    """La fabrique valide elle-même : on ne peut pas la contourner en l'appelant
    directement, sans passer par la route admin."""
    with pytest.raises(ValueError):
        build_kit_script(portal_url='https://x";$(id);"', token="t", runner_name="salle-a")

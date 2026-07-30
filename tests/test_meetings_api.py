"""Vague 3 — API des sessions de réunion : opt-in, permissions, machine d'états, claim.

Ce que ces tests prouvent : (1) la surface n'existe pas tant que la fonctionnalité est OFF ;
(2) `SCHEDULE_MEETINGS` gate la famille humaine, `OPERATE_MEETING_RUNNER` (attribution
NOMINATIVE par config, jamais par rôle) gate la famille runner ; (3) le cycle complet
planifier → claim → événements → résultat, avec les codes bot 0/1/2/3 ; (4) le claim
concurrent n'attribue jamais deux fois ; (5) la référence de réunion ne sort QUE par le
claim runner — jamais dans les réponses humaines.
"""
from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet


@pytest.fixture(autouse=True)
def _force_feature_baseline(app):
    """Leçon 0.3.5 : les tests gatés par config FORCENT leur baseline — sans quoi le
    config.yaml de la MACHINE (ex. façade/réunions activées pour un gate réel) change le
    comportement de la suite. OFF par défaut ici ; les fixtures d'activation surchargent."""
    from transcria.config import get_config
    cfg = get_config()
    prev = cfg.get("connectors")
    cfg.pop("connectors", None)
    # Isolation du fichier de jeton local : les tests ne touchent JAMAIS au instance/ réel
    # (vécu : le vrai jeton déposé par le bouton « Activer » du service, root 0600, entrait
    # en collision avec la suite).
    import tempfile
    from pathlib import Path as _P

    from transcria.ingestion import runner_provisioning as _rp
    with tempfile.TemporaryDirectory() as tmp_tokens:
        original = _rp._token_path
        _rp._token_path = lambda: _P(tmp_tokens) / "token.txt"
        try:
            yield
        finally:
            _rp._token_path = original
            if prev is None:
                cfg.pop("connectors", None)
            else:
                cfg["connectors"] = prev

# Clé STABLE pour tout le module : la base de test est partagée entre tests — une clé par
# test rendrait indéchiffrables les sessions créées par le test précédent.
_MODULE_KEY = Fernet.generate_key().decode()

from transcria.auth.api_tokens import create_token
from transcria.auth.models import Role
from transcria.auth.store import UserStore


@pytest.fixture
def meetings_on(app, monkeypatch):
    """Active la fonctionnalité + clé de chiffrement + un compte runner nominatif."""
    monkeypatch.setenv("TRANSCRIA_MEETING_REF_KEY", _MODULE_KEY)
    from transcria.config import get_config
    cfg = get_config()
    prev = cfg.get("connectors")
    cfg["connectors"] = {"meetings": {"enabled": True, "runner_usernames": ["svc-runner"]}}
    yield
    if prev is None:
        cfg.pop("connectors", None)
    else:
        cfg["connectors"] = prev


@pytest.fixture(autouse=True)
def _clean_sessions(app):
    """Isolation : chaque test part sans session ni runner résiduels (base partagée)."""
    yield
    with app.app_context():
        from transcria.database import db
        from transcria.ingestion.session_models import MeetingRunner, MeetingSession
        db.session.query(MeetingSession).delete()
        db.session.query(MeetingRunner).delete()
        db.session.commit()


@pytest.fixture
def runner_token(app):
    with app.app_context():
        user = UserStore.get_by_username("svc-runner")
        if user is None:
            user = UserStore.create_user("svc-runner", "x" * 24, role=Role.OPERATOR)
        full, _ = create_token(user.id, "runner-test")
        return full


@pytest.fixture
def op_token(app):
    with app.app_context():
        user = UserStore.create_user(f"meet-{uuid.uuid4().hex[:8]}", "x" * 24, role=Role.OPERATOR)
        full, _ = create_token(user.id, "meet-test")
        return full


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _heartbeat(client, runner_token, platforms=("jitsi",)):
    return client.post("/v1/runners/heartbeat", headers=_auth(runner_token),
                       json={"runner": "runner-1", "capacity": 2, "active": 0,
                             "platforms": list(platforms), "images": []})


def _create(client, admin_client, ref="https://meet.jit.si/salle-test"):
    return admin_client.post("/api/meetings", json={
        "provider": "jitsi", "meeting_ref": ref, "title": "Comité test", "language": "fr"})


class TestOptInAndPermissions:
    def test_off_par_defaut_404(self, client, admin_client):
        assert admin_client.get("/api/meetings/availability").status_code == 404
        assert client.post("/v1/meetings/claim", json={}).status_code == 404

    def test_availability_vide_sans_runner(self, meetings_on, admin_client):
        body = admin_client.get("/api/meetings/availability").get_json()
        assert body == {"providers": [], "runners": 0}

    def test_availability_apres_heartbeat(self, meetings_on, client, admin_client, runner_token):
        assert _heartbeat(client, runner_token).status_code == 200
        body = admin_client.get("/api/meetings/availability").get_json()
        assert body["runners"] == 1
        assert {p["id"] for p in body["providers"]} == {"jitsi"}   # validated + couvert

    def test_viewer_sans_permission_ne_voit_rien(self, meetings_on, viewer_client):
        body = viewer_client.get("/api/meetings/availability").get_json()
        assert body["providers"] == []            # pas de 403 : la carte est juste absente

    def test_jeton_ordinaire_refuse_cote_runner(self, meetings_on, client, op_token):
        r = client.post("/v1/meetings/claim", headers=_auth(op_token),
                        json={"runner": "x"})
        assert r.status_code == 403               # OPERATE_MEETING_RUNNER = nominatif config


class TestScheduleAndLifecycle:
    def test_cycle_complet_immediat(self, meetings_on, client, admin_client, runner_token, app):
        _heartbeat(client, runner_token)
        r = _create(client, admin_client)
        assert r.status_code == 201, r.get_json()
        body = r.get_json()
        session = body["session"]
        assert session["state"] == "planned"
        assert "meeting_ref" not in session       # JAMAIS la référence côté humain

    # provenance posée sur le job dès la planification
        with app.app_context():
            from transcria.jobs.store import JobStore
            extra = JobStore.get_by_id(body["job_id"]).get_extra_data()
            assert extra["source"] == "meeting" and extra["provider"] == "jitsi"

        claim = client.post("/v1/meetings/claim", headers=_auth(runner_token),
                            json={"runner": "runner-1", "max": 2}).get_json()
        assert len(claim["sessions"]) == 1
        intent = claim["sessions"][0]
        assert intent["meeting_ref"] == "https://meet.jit.si/salle-test"   # déchiffrée ICI seulement
        assert intent["owner_name"]                       # l'initiateur voyage jusqu'au bot

        sid = session["id"]
        for event in ("joining", "waiting_admission", "in_meeting", "ingesting"):
            r = client.post(f"/v1/meetings/{sid}/events", headers=_auth(runner_token),
                            json={"runner": "runner-1", "event": event})
            assert r.status_code == 200, (event, r.get_json())
        r = client.post(f"/v1/meetings/{sid}/result", headers=_auth(runner_token),
                        json={"runner": "runner-1", "exit_code": 0})
        assert r.status_code == 200
        with app.app_context():
            from transcria.ingestion.session_store import MeetingSessionStore
            assert MeetingSessionStore.get(sid).state == "done"

    def test_doublon_meme_reference_409(self, meetings_on, client, admin_client, runner_token):
        _heartbeat(client, runner_token)
        assert _create(client, admin_client, ref="https://meet.jit.si/dup").status_code == 201
        r = _create(client, admin_client, ref="https://meet.jit.si/DUP  ")
        assert r.status_code == 409               # empreinte normalisée, sans déchiffrer

    def test_non_admis_terminal_sans_rejeu(self, meetings_on, client, admin_client, runner_token, app):
        _heartbeat(client, runner_token)
        sid = _create(client, admin_client, ref="https://meet.jit.si/refus").get_json()["session"]["id"]
        client.post("/v1/meetings/claim", headers=_auth(runner_token),
                    json={"runner": "runner-1"})
        client.post(f"/v1/meetings/{sid}/result", headers=_auth(runner_token),
                    json={"runner": "runner-1", "exit_code": 1, "category": "admission",
                          "message": "salle d'attente expirée"})
        with app.app_context():
            from transcria.ingestion.session_store import MeetingSessionStore
            s = MeetingSessionStore.get(sid)
            assert s.state == "not_admitted" and "admission" in s.last_error
        # et rien à re-claimer
        claim = client.post("/v1/meetings/claim", headers=_auth(runner_token),
                            json={"runner": "runner-1"}).get_json()
        assert claim["sessions"] == []

    def test_incident_technique_redevient_claimable_apres_backoff(self, meetings_on, client,
                                                                  admin_client, runner_token, app):
        _heartbeat(client, runner_token)
        sid = _create(client, admin_client, ref="https://meet.jit.si/retry").get_json()["session"]["id"]
        client.post("/v1/meetings/claim", headers=_auth(runner_token), json={"runner": "runner-1"})
        client.post(f"/v1/meetings/{sid}/result", headers=_auth(runner_token),
                    json={"runner": "runner-1", "exit_code": 2, "category": "media",
                          "message": "transport coupé"})
        with app.app_context():
            from transcria.ingestion.session_store import MeetingSessionStore
            s = MeetingSessionStore.get(sid)
            assert s.state == "planned" and s.next_retry_at is not None and s.claimed_by is None
        # backoff : pas re-claimable tout de suite
        claim = client.post("/v1/meetings/claim", headers=_auth(runner_token),
                            json={"runner": "runner-1"}).get_json()
        assert claim["sessions"] == []

    def test_annulation_puis_runner_perime_refuse(self, meetings_on, client, admin_client,
                                                  runner_token):
        _heartbeat(client, runner_token)
        sid = _create(client, admin_client, ref="https://meet.jit.si/cancel").get_json()["session"]["id"]
        client.post("/v1/meetings/claim", headers=_auth(runner_token), json={"runner": "runner-1"})
        r = admin_client.post(f"/api/meetings/{sid}/cancel")
        assert r.status_code == 200
        assert r.get_json()["session"]["state"] == "cancelled"
        # le runner périmé n'écrase pas l'annulation
        r = client.post(f"/v1/meetings/{sid}/events", headers=_auth(runner_token),
                        json={"runner": "runner-1", "event": "joining"})
        assert r.status_code == 409

    def test_claim_concurrent_jamais_double(self, meetings_on, client, admin_client, runner_token):
        _heartbeat(client, runner_token)
        _create(client, admin_client, ref="https://meet.jit.si/course")
        a = client.post("/v1/meetings/claim", headers=_auth(runner_token),
                        json={"runner": "runner-A"}).get_json()["sessions"]
        b = client.post("/v1/meetings/claim", headers=_auth(runner_token),
                        json={"runner": "runner-B"}).get_json()["sessions"]
        assert len(a) + len(b) == 1


class TestIngestAttach:
    """D4 : le bot RATTACHE l'audio au job planifié — réservé au compte de service runner."""

    def _facade_on(self, app):
        from transcria.config import get_config
        cfg = get_config()
        cfg.setdefault("live", {}).setdefault("facade", {})["enabled"] = True
        return cfg

    def test_jeton_ordinaire_ne_rattache_pas(self, meetings_on, client, admin_client,
                                             op_token, runner_token, app):
        import io
        self._facade_on(app)
        _heartbeat(client, runner_token)
        job_id = _create(client, admin_client, ref="https://meet.jit.si/attach1").get_json()["job_id"]
        r = client.post("/v1/audio/ingest", headers=_auth(op_token),
                        data={"file": (io.BytesIO(b"RIFF0000WAVE"), "r.wav"), "job_id": job_id},
                        content_type="multipart/form-data")
        assert r.status_code == 403

    def _wire_attach(self, monkeypatch):
        from transcria.web import facade_api
        monkeypatch.setattr(facade_api.JobService, "upload", staticmethod(lambda *a, **k: {"ok": True}))
        monkeypatch.setattr(facade_api.JobService, "analyze", staticmethod(lambda *a, **k: {"ok": True}))
        monkeypatch.setattr(facade_api, "JobFilesystem",
                            type("FS", (), {"__init__": lambda s, *a: None,
                                            "get_original_audio_path": lambda s: "/tmp/a.wav"}))
        monkeypatch.setattr(facade_api.PipelineService, "estimate_profile_resources",
                            staticmethod(lambda cfg, p: {}))
        captured = {}

        class _Exec:
            def submit_process(self, jid, path, mode, **kw):
                captured["mode"] = mode
                return {"accepted": True}
        monkeypatch.setattr(facade_api, "get_job_executor", lambda: _Exec())
        return captured

    def test_profil_a_validations_le_wizard_reprend_la_main(self, meetings_on, client,
                                                            admin_client, runner_token, app,
                                                            monkeypatch):
        """Décision utilisateur 2026-07-29 : la réunion amène l'audio AU MÊME POINT qu'un
        upload — pas de pipeline automatique quand le profil exige l'humain (résumé,
        locuteurs, lexique) : les suggestions du manifeste servent AVANT le traitement."""
        import io
        self._facade_on(app)
        _heartbeat(client, runner_token)
        job_id = _create(client, admin_client, ref="https://meet.jit.si/attach2").get_json()["job_id"]
        client.post("/v1/meetings/claim", headers=_auth(runner_token), json={"runner": "runner-1"})
        captured = self._wire_attach(monkeypatch)
        r = client.post("/v1/audio/ingest", headers=_auth(runner_token),
                        data={"file": (io.BytesIO(b"RIFF0000WAVE"), "r.wav"), "job_id": job_id},
                        content_type="multipart/form-data")
        assert r.status_code == 202, r.get_json()
        assert r.get_json()["attached"] is True
        assert "mode" not in captured             # AUCUN pipeline lancé : l'humain d'abord

    def test_profil_sans_validation_part_tout_seul(self, meetings_on, client, admin_client,
                                                   runner_token, app, monkeypatch):
        import io
        self._facade_on(app)
        _heartbeat(client, runner_token)
        job_id = _create(client, admin_client, ref="https://meet.jit.si/attach3").get_json()["job_id"]
        with app.app_context():
            from transcria.jobs.store import JobStore as JS
            JS.update_extra_data(job_id, lambda e: {**e, "processing_profile_id": "srt_express"})
        client.post("/v1/meetings/claim", headers=_auth(runner_token), json={"runner": "runner-1"})
        captured = self._wire_attach(monkeypatch)
        r = client.post("/v1/audio/ingest", headers=_auth(runner_token),
                        data={"file": (io.BytesIO(b"RIFF0000WAVE"), "r.wav"), "job_id": job_id},
                        content_type="multipart/form-data")
        assert r.status_code == 202, r.get_json()
        assert "mode" in captured                 # SRT express : rien à valider → pipeline direct

    def test_job_sans_session_409(self, meetings_on, client, admin_client, runner_token, app):
        import io
        self._facade_on(app)
        with app.app_context():
            from transcria.jobs.store import JobStore as JS
            owner = UserStore.get_by_username("svc-runner")
            job_id = JS.create_job(owner.id, "upload nu").id
        r = client.post("/v1/audio/ingest", headers=_auth(runner_token),
                        data={"file": (io.BytesIO(b"RIFF0000WAVE"), "r.wav"), "job_id": job_id},
                        content_type="multipart/form-data")
        assert r.status_code == 409


class TestLeasesAndCancellations:
    """Lot 4a : baux de claim (un runner mort rend ses sessions) + canal d'annulation à chaud."""

    def test_claim_perime_redevient_claimable(self, meetings_on, client, admin_client,
                                              runner_token, app):
        from datetime import datetime, timedelta, timezone
        _heartbeat(client, runner_token)
        _create(client, admin_client, ref="https://meet.jit.si/lease")
        client.post("/v1/meetings/claim", headers=_auth(runner_token), json={"runner": "mort"})
        with app.app_context():
            from transcria.database import db
            from transcria.ingestion.session_models import MeetingSession
            from transcria.ingestion.session_store import MeetingSessionStore
            s = db.session.execute(db.select(MeetingSession)).scalars().first()
            s.claimed_at = datetime.now(timezone.utc) - timedelta(seconds=600)
            db.session.commit()
            assert MeetingSessionStore.release_expired_leases() == 1
            db.session.refresh(s)
            assert s.state == "planned" and s.claimed_by is None

    def test_in_meeting_bail_long_termine_honnetement(self, meetings_on, client, admin_client,
                                                      runner_token, app):
        from datetime import datetime, timedelta, timezone
        _heartbeat(client, runner_token)
        sid = _create(client, admin_client, ref="https://meet.jit.si/lost").get_json()["session"]["id"]
        client.post("/v1/meetings/claim", headers=_auth(runner_token), json={"runner": "runner-1"})
        for event in ("joining", "in_meeting"):
            client.post(f"/v1/meetings/{sid}/events", headers=_auth(runner_token),
                        json={"runner": "runner-1", "event": event})
        with app.app_context():
            from transcria.database import db
            from transcria.ingestion.session_store import MeetingSessionStore
            s = MeetingSessionStore.get(sid)
            s.claimed_at = datetime.now(timezone.utc) - timedelta(hours=9)
            db.session.commit()
            assert MeetingSessionStore.release_expired_leases() == 1
            db.session.refresh(s)
            assert s.state == "failed_final" and "peut-être" in s.last_error

    def test_heartbeat_rend_les_annulations_a_stopper(self, meetings_on, client, admin_client,
                                                      runner_token):
        _heartbeat(client, runner_token)
        sid = _create(client, admin_client, ref="https://meet.jit.si/stop").get_json()["session"]["id"]
        client.post("/v1/meetings/claim", headers=_auth(runner_token), json={"runner": "runner-1"})
        admin_client.post(f"/api/meetings/{sid}/cancel")
        body = _heartbeat(client, runner_token).get_json()
        assert body["cancelled_sessions"] == [sid]


class TestAdminOneClick:
    """Décision utilisateur 2026-07-29 : l'admin ne touche que l'interface — l'interrupteur
    auto-provisionne tout, la check-list dit quoi réparer, la révocation est précise."""

    def test_activer_provisionne_tout(self, app, admin_client, tmp_path, monkeypatch):
        monkeypatch.setenv("TRANSCRIA_MEETING_REF_KEY", _MODULE_KEY)
        # ConfigService écrit un YAML : rediriger vers un fichier jetable
        from transcria.services.config_service import ConfigService
        cfg_file = tmp_path / "config.yaml"
        monkeypatch.setattr(ConfigService, "get_path", staticmethod(lambda *a: str(cfg_file)))
        r = admin_client.post("/admin/connecteurs/meetings/toggle", data={"action": "enable"})
        assert r.status_code == 302
        with app.app_context():
            from transcria.auth.store import UserStore
            from transcria.ingestion.runner_provisioning import _token_path
            assert UserStore.get_by_username("svc-runner") is not None
            assert _token_path().exists()
            assert _token_path().read_text(encoding="utf-8").startswith("tia_")
        import yaml
        saved = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        assert saved["connectors"]["meetings"]["enabled"] is True
        assert "svc-runner" in saved["connectors"]["meetings"]["runner_usernames"]
        assert saved["live"]["facade"]["enabled"] is True      # dépendance masquée à l'admin

    def test_checklist_affichee_avec_remedes(self, app, admin_client):
        html = admin_client.get("/admin/connecteurs").data.decode()
        assert "Réunions en ligne" in html
        assert "Activer" in html or "Désactiver" in html
        assert "✗" in html or "✓" in html

    def test_revocation_precise_par_token_id(self, meetings_on, client, admin_client,
                                             runner_token, app):
        _heartbeat(client, runner_token)
        r = admin_client.post("/admin/connecteurs/runners/runner-1/revoke")
        assert r.status_code == 302
        # le battement suivant est refusé : le jeton est mort
        assert _heartbeat(client, runner_token).status_code == 401
        with app.app_context():
            from transcria.ingestion.session_store import MeetingSessionStore
            assert MeetingSessionStore.live_runners() == []


class TestReschedule:
    def test_echec_replanifiable_nouvelle_session_meme_job(self, meetings_on, client,
                                                           admin_client, runner_token, app):
        _heartbeat(client, runner_token)
        body = _create(client, admin_client, ref="https://meet.jit.si/resched").get_json()
        sid, job_id = body["session"]["id"], body["job_id"]
        client.post("/v1/meetings/claim", headers=_auth(runner_token), json={"runner": "runner-1"})
        client.post(f"/v1/meetings/{sid}/result", headers=_auth(runner_token),
                    json={"runner": "runner-1", "exit_code": 125, "category": "docker",
                          "message": "image de bot absente"})
        r = admin_client.post(f"/api/meetings/{sid}/reschedule")
        assert r.status_code == 201
        fresh = r.get_json()["session"]
        assert fresh["job_id"] == job_id and fresh["state"] == "planned"   # même job, préparatifs gardés

    def test_session_reussie_non_replanifiable(self, meetings_on, client, admin_client, runner_token):
        _heartbeat(client, runner_token)
        sid = _create(client, admin_client, ref="https://meet.jit.si/done-ok").get_json()["session"]["id"]
        client.post("/v1/meetings/claim", headers=_auth(runner_token), json={"runner": "runner-1"})
        client.post(f"/v1/meetings/{sid}/result", headers=_auth(runner_token),
                    json={"runner": "runner-1", "exit_code": 0})
        assert admin_client.post(f"/api/meetings/{sid}/reschedule").status_code == 409


def _stored_passcode(app, session_id):
    """Lit le code STOCKÉ (chiffré) — nécessite le contexte applicatif."""
    from transcria.ingestion.session_store import MeetingSessionStore

    with app.app_context():
        return MeetingSessionStore.get(session_id).meeting_passcode_encrypted


class TestSalleProtegee:
    """Code d'accès d'une salle (« mot de passe » Jitsi) — trou trouvé à la revue de
    complétude du 2026-07-30 : le bot DÉTECTAIT `password_required` sans qu'aucun chemin
    ne permette de fournir le code. Contrat : SECRET (chiffré au repos, jamais réaffiché,
    déchiffré au SEUL claim du runner) et FACULTATIF (salle ouverte = cas courant)."""

    @staticmethod
    def _create(admin_client, ref, passcode=None):
        body = {"provider": "jitsi", "meeting_ref": ref, "title": "Protégée", "language": "fr"}
        if passcode is not None:
            body["passcode"] = passcode
        return admin_client.post("/api/meetings", json=body)

    def test_code_chiffre_au_repos_et_absent_des_reponses(self, app, meetings_on, client,
                                                          admin_client, runner_token):
        _heartbeat(client, runner_token)
        r = self._create(admin_client, "https://meet.jit.si/salle-protegee", "s3cr3t-de-salle")
        assert r.status_code == 201
        assert "s3cr3t" not in str(r.get_json())      # jamais renvoyé

        stored = _stored_passcode(app, r.get_json()["session"]["id"])
        assert stored and stored.startswith("enc1:")         # chiffré au repos
        assert "s3cr3t-de-salle" not in stored

    def test_salle_ouverte_aucun_code_stocke(self, app, meetings_on, client, admin_client, runner_token):
        _heartbeat(client, runner_token)
        r = self._create(admin_client, "https://meet.jit.si/salle-ouverte")
        assert r.status_code == 201
        assert _stored_passcode(app, r.get_json()["session"]["id"]) is None  # NULL ≠ code vide

    def test_le_runner_recoit_le_code_dechiffre_au_claim(self, meetings_on, client,
                                                         admin_client, runner_token):
        _heartbeat(client, runner_token)
        self._create(admin_client, "https://meet.jit.si/claim-protegee", "abc-123")
        r = client.post("/v1/meetings/claim", headers=_auth(runner_token),
                        json={"runner": "runner-1", "max": 5})
        assert r.status_code == 200
        intents = [i for i in r.get_json()["sessions"]
                   if str(i["meeting_ref"]).endswith("claim-protegee")]
        assert intents and intents[0]["meeting_passcode"] == "abc-123"

    def test_salle_ouverte_code_vide_au_claim(self, meetings_on, client, admin_client, runner_token):
        _heartbeat(client, runner_token)
        self._create(admin_client, "https://meet.jit.si/claim-ouverte")
        r = client.post("/v1/meetings/claim", headers=_auth(runner_token),
                        json={"runner": "runner-1", "max": 5})
        intents = [i for i in r.get_json()["sessions"]
                   if str(i["meeting_ref"]).endswith("claim-ouverte")]
        assert intents and intents[0]["meeting_passcode"] == ""


class TestIngestPistesSeparees:
    """Vague 5, lot A (D5.2) : parts `track_<id>` + manifeste v2 — validation STRICTE
    tout-ou-rien. Toute incohérence rejette LES PISTES EN BLOC : l'ingestion continue en
    mode mix, le manifeste dégradé l'annonce (`tracks_degraded`), jamais un état à moitié."""

    def _manifest_v2(self, refs=("track_p1",)):
        parts = [{"id": f"p{i+1}", "name": f"Personne {i+1}", "kind": "unknown",
                  "speech_windows": [[float(i), float(i) + 2.0]], "track": ref}
                 for i, ref in enumerate(refs)]
        return {"version": 2, "source": "jitsi", "mix": "timeline_common",
                "participants": parts}

    def _post_attach(self, client, runner_token, job_id, manifest, tracks):
        import io
        import json as _json
        data = {"file": (io.BytesIO(b"RIFF0000WAVE"), "r.wav"), "job_id": job_id,
                "participants_manifest": (
                    io.BytesIO(_json.dumps(manifest).encode()), "participants_manifest.json")}
        for ref, payload in tracks.items():
            data[ref] = (io.BytesIO(payload), f"{ref}.wav")
        return client.post("/v1/audio/ingest", headers=_auth(runner_token), data=data,
                           content_type="multipart/form-data")

    def _setup(self, client, admin_client, runner_token, app, monkeypatch, ref):
        from transcria.config import get_config
        with app.app_context():
            get_config().setdefault("live", {}).setdefault("facade", {})["enabled"] = True
        _heartbeat(client, runner_token)
        job_id = _create(client, admin_client, ref=ref).get_json()["job_id"]
        client.post("/v1/meetings/claim", headers=_auth(runner_token), json={"runner": "runner-1"})
        from transcria.web import facade_api
        monkeypatch.setattr(facade_api.JobService, "upload",
                            staticmethod(lambda *a, **k: {"ok": True}))
        monkeypatch.setattr(facade_api.JobService, "analyze",
                            staticmethod(lambda *a, **k: {"ok": True}))
        return job_id

    def _stored(self, app, job_id):
        from pathlib import Path

        from transcria.config import get_config
        with app.app_context():
            base = Path(get_config()["storage"]["jobs_dir"]) / job_id
        tracks = sorted(p.name for p in (base / "input" / "tracks").glob("*.wav")) \
            if (base / "input" / "tracks").exists() else []
        import json as _json
        mpath = base / "metadata" / "participants_manifest.json"
        manifest = _json.loads(mpath.read_text()) if mpath.exists() else None
        return tracks, manifest

    def test_pistes_stockees_et_manifeste_v2_conserve(self, meetings_on, client, admin_client,
                                                      runner_token, app, monkeypatch):
        job_id = self._setup(client, admin_client, runner_token, app, monkeypatch,
                             "https://meet.jit.si/tracks-ok")
        r = self._post_attach(client, runner_token, job_id,
                              self._manifest_v2(("track_p1", "track_p2")),
                              {"track_p1": b"WAV1", "track_p2": b"WAV2"})
        assert r.status_code == 202, r.get_json()
        tracks, manifest = self._stored(app, job_id)
        assert tracks == ["p1.wav", "p2.wav"]
        assert manifest["version"] == 2
        assert manifest["participants"][0]["track"] == "track_p1"

    def test_part_orpheline_rejette_tout_en_bloc(self, meetings_on, client, admin_client,
                                                 runner_token, app, monkeypatch):
        """Une part sans référence = incohérence bot : mode mix, dégradation ANNONCÉE."""
        job_id = self._setup(client, admin_client, runner_token, app, monkeypatch,
                             "https://meet.jit.si/tracks-orphan")
        r = self._post_attach(client, runner_token, job_id, self._manifest_v2(("track_p1",)),
                              {"track_p1": b"WAV1", "track_intrus": b"WAVX"})
        assert r.status_code == 202
        tracks, manifest = self._stored(app, job_id)
        assert tracks == []                        # tout-ou-rien : AUCUNE piste stockée
        assert manifest["version"] == 1
        assert manifest["tracks_degraded"] is True
        assert "track" not in manifest["participants"][0]

    def test_piste_trop_grosse_rejette_tout(self, meetings_on, client, admin_client,
                                            runner_token, app, monkeypatch):
        from transcria.config import get_config
        with app.app_context():
            get_config().setdefault("connectors", {}).setdefault("meetings", {})["max_track_mb"] = 0
        job_id = self._setup(client, admin_client, runner_token, app, monkeypatch,
                             "https://meet.jit.si/tracks-big")
        r = self._post_attach(client, runner_token, job_id, self._manifest_v2(("track_p1",)),
                              {"track_p1": b"X" * 1024})
        assert r.status_code == 202
        tracks, manifest = self._stored(app, job_id)
        assert tracks == [] and manifest["tracks_degraded"] is True

    def test_sans_pistes_comportement_historique(self, meetings_on, client, admin_client,
                                                 runner_token, app, monkeypatch):
        """Règle D5.2 : sans parts de piste, rien ne change — bots anciens et connecteurs
        post-réunion restent intacts."""
        job_id = self._setup(client, admin_client, runner_token, app, monkeypatch,
                             "https://meet.jit.si/tracks-none")
        v1 = {"version": 1, "source": "jitsi", "mix": "timeline_common",
              "participants": [{"id": "p1", "name": "Ana", "kind": "solo",
                                "speech_windows": [[0.0, 2.0]]}]}
        r = self._post_attach(client, runner_token, job_id, v1, {})
        assert r.status_code == 202
        tracks, manifest = self._stored(app, job_id)
        assert tracks == [] and manifest == v1


class TestLiveCaptions:
    """Vague 5, lot C (D5.5) : le runner claimant relaie les tours PROVISOIRES par lots ;
    la page du job les lit en delta avec la visibilité du job porteur ; le plafond tronque
    la tête EN L'ANNONÇANT. Jamais la référence : le batch la produira."""

    def _in_meeting_session(self, client, admin_client, runner_token):
        _heartbeat(client, runner_token)
        sid = _create(client, admin_client).get_json()["session"]["id"]
        client.post("/v1/meetings/claim", headers=_auth(runner_token),
                    json={"runner": "runner-1", "max": 1})
        for event in ("joining", "in_meeting"):
            client.post(f"/v1/meetings/{sid}/events", headers=_auth(runner_token),
                        json={"runner": "runner-1", "event": event})
        return sid

    def _post_captions(self, client, runner_token, sid, captions, runner="runner-1"):
        return client.post(f"/v1/meetings/{sid}/captions", headers=_auth(runner_token),
                           json={"runner": runner, "captions": captions})

    def test_relai_puis_lecture_en_delta(self, meetings_on, client, admin_client, runner_token):
        sid = self._in_meeting_session(client, admin_client, runner_token)
        r = self._post_captions(client, runner_token, sid, [
            {"start": 1.0, "end": 2.0, "speaker": "Alice", "text": "Bonjour"},
            {"start": 3.0, "end": 4.0, "speaker": "", "text": "Oui"},
            {"text": "   "},                                   # douteuse : écartée, pas d'échec
        ])
        assert r.status_code == 200 and r.get_json()["accepted"] == 2

        body = admin_client.get(f"/api/meetings/{sid}/captions?after=0").get_json()
        assert [c["text"] for c in body["captions"]] == ["Bonjour", "Oui"]
        assert body["state"] == "in_meeting" and body["truncated"] == 0
        delta = admin_client.get(f"/api/meetings/{sid}/captions?after={body['next']}").get_json()
        assert delta["captions"] == []                         # delta : rien de neuf

    def test_runner_perime_et_session_close_409(self, meetings_on, client, admin_client,
                                                runner_token):
        sid = self._in_meeting_session(client, admin_client, runner_token)
        r = self._post_captions(client, runner_token, sid,
                                [{"text": "x", "start": 0, "end": 1}], runner="autre")
        assert r.status_code == 409                            # claimée par un autre exécutant
        client.post(f"/v1/meetings/{sid}/events", headers=_auth(runner_token),
                    json={"runner": "runner-1", "event": "ingesting"})
        r = self._post_captions(client, runner_token, sid, [{"text": "tard", "start": 0, "end": 1}])
        assert r.status_code == 409                            # le direct est clos à l'ingestion

    def test_visibilite_celle_du_job_porteur(self, meetings_on, client, admin_client,
                                             runner_token, viewer_client):
        sid = self._in_meeting_session(client, admin_client, runner_token)
        assert viewer_client.get(f"/api/meetings/{sid}/captions").status_code == 404

    def test_plafond_tronque_et_annonce(self, meetings_on, client, admin_client, runner_token):
        from transcria.config import get_config
        get_config()["connectors"]["meetings"]["max_caption_lines"] = 5
        sid = self._in_meeting_session(client, admin_client, runner_token)
        self._post_captions(client, runner_token, sid,
                            [{"start": i, "end": i + 1, "text": f"tour {i}"} for i in range(8)])
        body = admin_client.get(f"/api/meetings/{sid}/captions?after=0").get_json()
        assert body["truncated"] == 3 and len(body["captions"]) == 5
        assert body["captions"][0]["n"] == 4                   # la tête est partie, n monotone

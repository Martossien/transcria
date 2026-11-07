"""Chaîne complète du remède automatique (2026-11-07) : runner → phase résumé → doctor → écran.

Le module pur est couvert par test_opencode_remediation.py ; ici, les COUTURES : le runner classe
la panne et rejoue UNE fois après bascule, la phase résumé ne retente pas une panne déterministe,
le doctor avertit « v1 + cloud », le module de smoke mesure, l'écran teste/bascule/installe/annule.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from builders import make_config

from transcria.audit.models import AuditAction, AuditLog
from transcria.database import db
from transcria.llm_tools import opencode_remediation as R
from transcria.llm_tools import opencode_smoke as S
from transcria.llm_tools.opencode_cli import OpencodeVersion
from transcria.llm_tools.opencode_runner import OpenCodeRunner
from transcria.web import llm_cloud_routes
from transcria.web import routes as web_routes

V1 = OpencodeVersion(1, 18, 22, raw="1.18.22")
V2 = OpencodeVersion(2, 2, 6, raw="2.2.6")
STDERR_SIG = ('error="Type validation failed: Value: {"choices":[{"delta":{"tool_calls":[{"function":{}}]}}]}. '
              'Error message: path ["choices",0,"delta","tool_calls",0,"id"] expected string, received undefined"')


def _runner(tmp_path, cfg=None) -> OpenCodeRunner:
    cfg = cfg or {"workflow": {"arbitration_llm": {"model_id": "mistral/zai-glm-5-3", "opencode_bin": "/v1/opencode"}}}
    return OpenCodeRunner(str(tmp_path), config=cfg)


# ── runner ───────────────────────────────────────────────────────────────────

def test_failure_classee_porte_la_cause_et_la_signature():
    res = OpenCodeRunner._failure("opencode exit 1: …", STDERR_SIG, [], V1)
    assert res["error_kind"] == R.SDK_TOOL_CALL_INCOMPAT and res["success"] is False
    assert res["error"].startswith("le binaire opencode utilisé (v1.18.22)") and "Détail : opencode exit 1" in res["error"]
    assert res["error_signature"] == "opencode exit 1: …"
    generic = OpenCodeRunner._failure("opencode exit 1: boom", "rien de connu", [], V1)
    assert generic == {"success": False, "error_kind": "opencode_error", "error": "opencode exit 1: boom"}


def test_run_rejoue_une_fois_apres_bascule_automatique(tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    calls: list[str] = []

    def fake_once(instruction, prompt_file, timeout=600):
        calls.append(runner.opencode_bin)
        runner._last_binary, runner._last_version = runner.opencode_bin, (V1 if runner.opencode_bin == "/v1/opencode" else V2)
        if runner.opencode_bin == "/v1/opencode":
            return {"success": False, "error_kind": R.SDK_TOOL_CALL_INCOMPAT, "error": "cause"}
        return {"success": True, "output": "Fait.", "files": [], "events_count": 3, "tool_calls": 1}

    monkeypatch.setattr(runner, "_run_once", fake_once)
    seen: dict = {}

    def fake_switch(config, *, current_bin, current_version, kind, version_reader, config_path=None):
        seen.update(bin=current_bin, version=current_version, kind=kind)
        return {"from": current_bin, "to": "/v2/opencode", "at": "t", "reason": kind, "automatic": True, "persisted": True}

    monkeypatch.setattr("transcria.llm_tools.opencode_runner.auto_switch_for", fake_switch)
    res = runner.run("x", "/p.txt")
    assert res["success"] is True and calls == ["/v1/opencode", "/v2/opencode"]
    assert seen == {"bin": "/v1/opencode", "version": V1, "kind": R.SDK_TOOL_CALL_INCOMPAT}
    assert runner.auto_switch and runner.auto_switch["to"] == "/v2/opencode" and runner.opencode_bin == "/v2/opencode"


def test_run_sans_v2_rend_la_cause_sans_rejouer(tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    calls = []
    monkeypatch.setattr(runner, "_run_once", lambda *a, **k: calls.append(1) or
                        {"success": False, "error_kind": R.SDK_TOOL_CALL_INCOMPAT, "error": "cause nommée"})
    monkeypatch.setattr("transcria.llm_tools.opencode_runner.auto_switch_for", lambda *a, **k: None)
    res = runner.run("x", "/p.txt")
    assert res["error_kind"] == R.SDK_TOOL_CALL_INCOMPAT and len(calls) == 1 and runner.auto_switch is None


def test_run_generique_ne_cherche_pas_de_remede(tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    monkeypatch.setattr(runner, "_run_once", lambda *a, **k: {"success": False, "error_kind": "opencode_error", "error": "e"})
    monkeypatch.setattr("transcria.llm_tools.opencode_runner.auto_switch_for",
                        lambda *a, **k: pytest.fail("pas de bascule pour une panne générique"))
    assert runner.run("x", "/p.txt")["error_kind"] == "opencode_error"


def test_run_summary_propage_la_classe_reconnue(tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    monkeypatch.setattr(runner, "run", lambda *a, **k: {"success": False, "error_kind": R.SDK_TOOL_CALL_INCOMPAT, "error": "cause"})
    parsed = runner.run_summary("/tmp/transcript.txt")
    assert parsed["_failure_kind"] == R.SDK_TOOL_CALL_INCOMPAT and parsed["_failure_detail"] == "cause"


# ── phase résumé : pas de nouvel essai sur une panne déterministe ────────────

def test_resume_ne_retente_pas_une_panne_reconnue(monkeypatch):
    from transcria.workflow.phases import summary_llm as SL

    monkeypatch.setattr(SL, "resolve_output_language", lambda job: "fr")
    calls = []
    ocr = SimpleNamespace(run_summary=lambda *a, **k: calls.append(1) or {
        "summary_text": "Résumé indisponible.", "_failure_kind": R.SDK_TOOL_CALL_INCOMPAT, "_failure_detail": "cause"})
    runner = SimpleNamespace(_summary_usable=lambda p: False,
                             vram=SimpleNamespace(ensure_arbitrage_llm_ready=lambda *a, **k: pytest.fail("ne relance pas")))
    logs = []
    sl = SimpleNamespace(info=lambda *a: None, warning=lambda *a: None, error=lambda msg, *a: logs.append(msg % a))
    parsed = SL._invoke_llm_with_retries(runner, ocr, SimpleNamespace(id="j"), sl, audio_duration_s=0.0,
                                         staged_transcript="t", staged_context="c", staged_diar_ctx="d", staged_invite=None,
                                         prompt_subs={}, extract_keys=(), api_model_id="arbitrage", config={})
    assert len(calls) == 1 and parsed["_failure_kind"] == R.SDK_TOOL_CALL_INCOMPAT
    assert logs and "pas de nouvel essai" in logs[0]


# ── module de smoke ──────────────────────────────────────────────────────────

def _factory(result, *, write_smoke=None, switched=None):
    class _Runner:
        def __init__(self, work_dir, config=None):
            self.work_dir = work_dir
            self.auto_switch = switched

        def run(self, instruction, prompt_file, timeout=600):
            assert Path(prompt_file).read_text(encoding="utf-8")
            if write_smoke is not None:
                (Path(self.work_dir) / "smoke.md").write_text(write_smoke, encoding="utf-8")
            return result
    return _Runner


def test_smoke_mesure_et_classe():
    cfg = {"workflow": {"arbitration_llm": {"smoke_timeout_seconds": "7"}}}
    assert S.smoke_timeout_seconds(cfg) == 7 and S.smoke_timeout_seconds({}) == 120
    ok = S.run_opencode_smoke(cfg, runner_factory=_factory({"success": True, "output": "Fait."}))
    assert ok.ok and ok.produced_text and not ok.produced_file and ok.seconds >= 0
    by_file = S.run_opencode_smoke(cfg, runner_factory=_factory({"success": True, "output": ""}, write_smoke="OK"))
    assert by_file.ok and by_file.produced_file
    nothing = S.run_opencode_smoke(cfg, runner_factory=_factory({"success": True, "output": "", "events_count": 9}))
    assert not nothing.ok and nothing.no_production and nothing.details["events_count"] == 9
    rec = {"from": "/v1", "to": "/v2", "at": "t", "reason": "r", "automatic": True, "persisted": True}
    failed = S.run_opencode_smoke(cfg, runner_factory=_factory(
        {"success": False, "error_kind": R.SDK_TOOL_CALL_INCOMPAT, "error": "cause", "error_signature": "sig"}, switched=rec))
    assert not failed.ok and failed.error_kind == R.SDK_TOOL_CALL_INCOMPAT and failed.switched == rec
    assert failed.details["signature"] == "sig" and not failed.no_production


# ── doctor : v1 + fournisseur cloud ──────────────────────────────────────────

def test_doctor_avertit_v1_avec_fournisseur_cloud():
    from transcria.diagnostics import doctor as doc

    cloud = {"services": {"backend": "cloud"},
             "workflow": {"summary_llm": {"enabled": True}, "arbitration_llm": {"model_id": "mistral/zai-glm-5-3", "opencode_bin": "/x"}}}
    finder = lambda config_bin=None: "/x/opencode"  # noqa: E731
    warn = doc.check_opencode(cloud, finder=finder, version_reader=lambda p: "1.18.22")
    assert warn.status == doc.WARN and "ligne v1" in warn.detail and "LLM d'arbitrage" in (warn.hint or "")
    ok = doc.check_opencode(cloud, finder=finder, version_reader=lambda p: "opencode v2.2.6")
    assert ok.status == doc.OK
    local = {**cloud, "services": {"backend": "script"}}
    assert doc.check_opencode(local, finder=finder, version_reader=lambda p: "1.18.22").status == doc.OK


def test_doctor_smoke_nomme_la_classe_reconnue_et_la_bascule():
    from transcria.diagnostics import doctor as doc

    cfg = {"services": {"backend": "cloud"},
           "workflow": {"summary_llm": {"enabled": True}, "arbitration_llm": {"model_id": "mistral/zai-glm-5-3"}}}
    rec = {"from": "/v1", "to": "/v2/opencode", "at": "t", "reason": "r", "automatic": True, "persisted": True}
    res = doc.check_opencode_smoke(cfg, runner_factory=_factory(
        {"success": False, "error_kind": R.SDK_TOOL_CALL_INCOMPAT, "error": "cause"}), notify=lambda m: None)
    assert res.status == doc.FAIL and "ligne v1" in res.detail and "Installer opencode v2" in (res.hint or "")
    res = doc.check_opencode_smoke(cfg, runner_factory=_factory({"success": True, "output": "Fait."}, switched=rec),
                                   notify=lambda m: None)
    assert res.status == doc.OK and "/v2/opencode" in res.detail and "annulable" in res.detail


# ── écran ────────────────────────────────────────────────────────────────────

@pytest.fixture
def cloud_v1(monkeypatch):
    cfg = make_config({"services": {"backend": "cloud"},
                       "workflow": {"arbitration_llm": {"enabled": True, "model_id": "mistral/modele-remede",
                                                        "opencode_bin": "/v1/opencode"},
                                    "summary_llm": {"enabled": True, "model_id": "mistral/modele-remede"}}})
    monkeypatch.setattr(llm_cloud_routes, "get_config", lambda: cfg)
    monkeypatch.setattr(web_routes, "get_config", lambda: cfg)
    monkeypatch.setattr(llm_cloud_routes, "find_opencode_binary", lambda config_bin=None: "/v1/opencode")
    monkeypatch.setattr(OpenCodeRunner, "opencode_version", staticmethod(lambda p: V2 if "/v2/" in p else V1))
    monkeypatch.setattr(R, "find_v2_binary", lambda **kw: ("/v2/opencode", V2))
    return cfg


def _rows(app, action: AuditAction) -> list:
    with app.app_context():
        return db.session.execute(db.select(AuditLog).filter_by(action=action.value)).scalars().all()


def test_ecran_montre_la_carte_opencode_et_les_remedes(admin_client, cloud_v1):
    html = admin_client.get("/admin/llm-cloud").get_data(as_text=True)
    assert "data-opencode-card" in html and "data-opencode-v1-warning" in html
    assert "/v1/opencode" in html and "ligne</span>" not in html
    assert "Tester la LLM maintenant" in html and "Installer opencode v2" in html
    assert "/v2/opencode" in html and "Utiliser l'opencode v2.2.6 trouvé" in html
    assert 'name="run_test"' in html                                   # test proposé à la validation


def test_route_test_traduit_le_resultat_et_audite(app, admin_client, cloud_v1, monkeypatch):
    monkeypatch.setattr(llm_cloud_routes, "run_opencode_smoke", lambda cfg: S.SmokeOutcome(True, 4.2, produced_text=True))
    html = admin_client.post("/admin/llm-cloud/test", follow_redirects=True).get_data(as_text=True)
    assert "LLM testée avec succès" in html and "4.2" in html
    rows = _rows(app, AuditAction.CONFIG_LLM_TEST)
    assert rows and '"ok": true' in rows[-1].details_json
    monkeypatch.setattr(llm_cloud_routes, "run_opencode_smoke",
                        lambda cfg: S.SmokeOutcome(False, 5.0, error="cause", error_kind=R.SDK_TOOL_CALL_INCOMPAT,
                                                   switched={"from": "/v1/opencode", "to": "/v2/opencode", "at": "t", "reason": "r"}))
    resp = admin_client.post("/admin/llm-cloud/test")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200 and "ligne v1" in html and "basculé automatiquement vers /v2/opencode" in html
    monkeypatch.setattr(llm_cloud_routes, "run_opencode_smoke", lambda cfg: S.SmokeOutcome(False, 3.0))
    assert "sans produire de texte" in admin_client.post("/admin/llm-cloud/test").get_data(as_text=True)


def test_route_test_refusee_sur_le_tier_web(app, admin_client, cloud_v1, monkeypatch):
    monkeypatch.setitem(app.config, "TRANSCRIA_ROLE", "web")
    monkeypatch.setattr(llm_cloud_routes, "run_opencode_smoke", lambda cfg: pytest.fail("pas de test sur le tier web"))
    resp = admin_client.post("/admin/llm-cloud/test")
    assert resp.status_code == 400 and "orchestrateur" in resp.get_data(as_text=True)
    assert "data-opencode-web-tier" in admin_client.get("/admin/llm-cloud").get_data(as_text=True)


def test_validation_lance_le_test_seulement_si_demande(admin_client, cloud_v1, monkeypatch):
    calls = []
    monkeypatch.setattr(llm_cloud_routes, "run_opencode_smoke",
                        lambda cfg: calls.append(1) or S.SmokeOutcome(True, 2.0, produced_text=True))
    admin_client.post("/admin/llm-cloud/acknowledge", data={})
    assert calls == []
    html = admin_client.post("/admin/llm-cloud/acknowledge", data={"run_test": "1"}, follow_redirects=True).get_data(as_text=True)
    assert calls == [1] and "Validation enregistrée" in html and "LLM testée avec succès" in html


def test_route_switch_redecouvre_et_bascule(admin_client, cloud_v1, monkeypatch):
    seen = {}

    def fake_switch(cfg, new_bin, *, previous, reason, actor_id=None, actor_username="", automatic=True, **kw):
        seen.update(to=new_bin, previous=previous, automatic=automatic, actor=actor_username)
        return {"from": previous, "to": new_bin, "at": "t", "reason": reason, "automatic": automatic, "persisted": True}

    monkeypatch.setattr(R, "switch_binary", fake_switch)
    resp = admin_client.post("/admin/llm-cloud/opencode/switch", data={"binary": "/ailleurs/opencode"})
    assert resp.status_code == 400 and not seen                      # un chemin libre n'est jamais accepté
    html = admin_client.post("/admin/llm-cloud/opencode/switch", data={"binary": "/v2/opencode"},
                             follow_redirects=True).get_data(as_text=True)
    assert seen == {"to": "/v2/opencode", "previous": "/v1/opencode", "automatic": False, "actor": seen["actor"]}
    assert "opencode v2 utilisé" in html and "sans redémarrage" in html


def test_route_install_pose_la_copie_privee_puis_bascule(app, admin_client, cloud_v1, monkeypatch):
    installed = {}

    def fake_install(*, opencode_home, version, modify_path, run, **kw):
        installed.update(home=str(opencode_home), version=version, modify_path=modify_path)
        return True

    monkeypatch.setattr(llm_cloud_routes, "install_opencode_binary", fake_install)
    monkeypatch.setattr(R, "switch_binary", lambda cfg, new_bin, **kw: {"from": "/v1/opencode", "to": new_bin, "at": "t",
                                                                      "reason": "i", "automatic": False, "persisted": True})
    html = admin_client.post("/admin/llm-cloud/opencode/install", follow_redirects=True).get_data(as_text=True)
    assert installed["version"].startswith("2.") and installed["modify_path"] is False
    assert installed["home"].endswith("/.transcria/opencode")
    assert "installé" in html and "/.transcria/opencode/.opencode/bin/opencode" in html
    rows = _rows(app, AuditAction.CONFIG_OPENCODE_INSTALL)
    assert rows and '"ok": true' in rows[-1].details_json
    monkeypatch.setattr(llm_cloud_routes, "install_opencode_binary", lambda **kw: False)
    resp = admin_client.post("/admin/llm-cloud/opencode/install")
    assert resp.status_code == 500 and "opencode.ai/v2/install" in resp.get_data(as_text=True)


def test_route_revert_annule_ou_dit_qu_il_n_y_a_rien(admin_client, cloud_v1, monkeypatch):
    monkeypatch.setattr(R, "revert_switch", lambda cfg, **kw: "/v1/opencode")
    html = admin_client.post("/admin/llm-cloud/opencode/revert", follow_redirects=True).get_data(as_text=True)
    assert "Bascule annulée" in html and "/v1/opencode" in html
    monkeypatch.setattr(R, "revert_switch", lambda cfg, **kw: None)
    assert admin_client.post("/admin/llm-cloud/opencode/revert").status_code == 400


def test_bascule_enregistree_affichee_avec_annulation(admin_client, cloud_v1, monkeypatch):
    monkeypatch.setattr(R, "pending_switch", lambda cfg: {"from": "/v1/opencode", "to": "/v2/opencode",
                                                          "at": "2026-11-07T21:00:00+00:00", "reason": "sdk_tool_call_incompat"})
    html = admin_client.get("/admin/llm-cloud").get_data(as_text=True)
    assert "data-opencode-switched" in html and "Annuler la bascule" in html and "2026-11-07 21:00" in html


def test_routes_exigent_manage_config(client, cloud_v1):
    for url in ("/admin/llm-cloud/test", "/admin/llm-cloud/opencode/switch",
                "/admin/llm-cloud/opencode/install", "/admin/llm-cloud/opencode/revert"):
        assert client.post(url).status_code in (302, 401, 403), url

"""Doctor — LLM d'arbitrage cloud (docs/LLM_CLOUD.md § 3.3.1, lot C).

Toutes les sondes sont injectées : aucun réseau, aucun binaire, aucune base réelle (sauf le
test du lecteur SQL, sur une base SQLite jetable).
"""
from __future__ import annotations

import signal
import sqlite3
import subprocess

import pytest
from builders import make_config

from transcria.diagnostics import doctor as doc
from transcria.diagnostics.checks import llm_cloud as lc
from transcria.llm_cloud.policy import CLOUD_NOTICE_TEXT_VERSION

SECRET = "sk-secret-VALEUR-123"
MODEL_ID = "mistral/mistral-large-4"


def _cloud_cfg(**overrides) -> dict:
    base = {
        "services": {"backend": "cloud"},
        "workflow": {
            "arbitration_llm": {"enabled": True, "model_id": MODEL_ID},
            "summary_llm": {"enabled": True, "model_id": MODEL_ID},
        },
    }
    return make_config(_merge(base, overrides))


def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _valid_ack(cfg, provider, model):
    return {"provider": provider, "model": model, "text_version": CLOUD_NOTICE_TEXT_VERSION, "revoked_at": None}


def _probes(**over):
    """Sondes nominales (tout est vert) ; chaque test en remplace une."""
    probes = {
        "opencode_config_path": "/x/opencode.json",
        "opencode_config_reader": lambda path: {"share": "disabled", "provider": {"local": {"models": {}}}},
        "auth_path": "/x/auth.json",
        "auth_reader": lambda path: None,
        "db_path": "/x/opencode.db",
        "db_reader": lambda path: None,
        "catalog_probe": lambda cfg: [MODEL_ID, "mistral/autre"],
        "provider_probe": lambda cfg: (["mistral-large-4", "autre"], None),
        "ack_reader": _valid_ack,
    }
    probes.update(over)
    return probes


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", SECRET)


def _assert_no_secret(res):
    assert SECRET not in res.detail
    assert SECRET not in (res.hint or "")


# ── Hors cloud ───────────────────────────────────────────────────────────────


def test_not_cloud_is_ok_not_applicable():
    res = lc.check_llm_cloud(make_config(), **_probes(catalog_probe=_boom, provider_probe=_boom, ack_reader=_boom))
    assert res.status == doc.OK
    assert "sans objet" in res.detail


def _boom(*args, **kwargs):
    raise AssertionError("aucune sonde ne doit tourner hors cloud")


def test_cloud_with_llm_disabled_is_ok():
    cfg = _cloud_cfg(workflow={"arbitration_llm": {"enabled": False}, "summary_llm": {"enabled": False}})
    assert lc.check_llm_cloud(cfg, **_probes(ack_reader=_boom)).status == doc.OK


# ── Nominal ──────────────────────────────────────────────────────────────────


def test_nominal_ok_names_provider_model_url():
    res = lc.check_llm_cloud(_cloud_cfg(), **_probes())
    assert res.status == doc.OK, res
    assert "mistral" in res.detail and "mistral-large-4" in res.detail
    assert "https://api.mistral.ai/v1" in res.detail
    _assert_no_secret(res)


# ── Ce que la sonde GET /models PROUVE, selon le fournisseur (mesuré 2026-11-01) ──


def test_liste_exigeant_la_cle_un_200_vaut_cle_acceptee():
    res = lc.check_llm_cloud(_cloud_cfg(), **_probes())
    assert res.status == doc.OK
    assert "clé MISTRAL_API_KEY acceptée" in res.detail
    assert "NON vérifiée" not in res.detail


def _ollama_cfg():
    mid = "ollama-cloud/glm-5.2"
    return _cloud_cfg(workflow={"arbitration_llm": {"model_id": mid}, "summary_llm": {"model_id": mid}})


def test_liste_publique_ne_dit_jamais_cle_valide(monkeypatch):
    """Ollama cloud : GET /v1/models répond 200 même avec une clé bidon. Un doctor qui en
    déduirait « clé valide » mentirait sur une clé révoquée — il dit « présente, NON vérifiée »."""
    monkeypatch.setenv("OLLAMA_API_KEY", SECRET)
    res = lc.check_llm_cloud(_ollama_cfg(), **_probes(
        catalog_probe=lambda c: ["ollama-cloud/glm-5.2"], provider_probe=lambda c: (["glm-5.2", "kimi-k3"], None)))
    assert res.status == doc.OK, res
    assert "clé OLLAMA_API_KEY présente mais NON vérifiée" in res.detail
    assert "acceptée" not in res.detail
    assert "--llm-smoke" in res.detail
    _assert_no_secret(res)


def test_liste_publique_garde_ses_verdicts_modele_et_401(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", SECRET)
    probes = _probes(catalog_probe=lambda c: ["ollama-cloud/glm-5.2"])
    absent = lc.check_llm_cloud(_ollama_cfg(), **{**probes, "provider_probe": lambda c: (["autre"], None)})
    assert absent.status == doc.FAIL and "glm-5.2" in absent.detail
    refuse = lc.check_llm_cloud(_ollama_cfg(), **{**probes, "provider_probe": lambda c: (None, "statut HTTP 401")})
    assert refuse.status == doc.FAIL and "refuse la clé" in refuse.detail


def test_fournisseur_sans_liste_ne_sonde_pas_et_le_dit(monkeypatch):
    """opencode : pas de GET /models (404 mesuré). Aucune sonde ; ni clé ni modèle vérifiables."""
    monkeypatch.setenv("OPENCODE_API_KEY", SECRET)
    mid = "opencode/un-modele"
    cfg = _cloud_cfg(workflow={"arbitration_llm": {"model_id": mid}, "summary_llm": {"model_id": mid}})
    res = lc.check_llm_cloud(cfg, **_probes(catalog_probe=lambda c: [mid], provider_probe=_boom))
    assert res.status == doc.OK, res
    assert "n'expose pas de liste des modèles" in res.detail
    assert "--llm-smoke" in res.detail
    assert "acceptée" not in res.detail
    _assert_no_secret(res)


def test_fournisseur_hors_table_ne_presume_rien_sur_la_cle(monkeypatch):
    monkeypatch.setenv("MAISON_KEY", SECRET)
    cfg = _cloud_cfg(workflow={
        "arbitration_llm": {"model_id": "maison/m1", "api_base": "https://llm.exemple.test/v1"},
        "summary_llm": {"model_id": "maison/m1"},
        "llm_cloud": {"api_key_env": "MAISON_KEY"},
    })
    res = lc.check_llm_cloud(cfg, **_probes(catalog_probe=lambda c: ["maison/m1"], provider_probe=lambda c: (["m1"], None)))
    assert res.status == doc.OK, res
    assert "hors table" in res.detail and "acceptée" not in res.detail


def test_fournisseur_non_sonde_verdict_non_verifiee():
    res = lc.check_llm_cloud(_cloud_cfg(), **_probes(provider_probe=lambda c: (None, "connexion refusée/coupée: x")))
    assert res.status == doc.WARN
    assert "non vérifiée (fournisseur non sondé)" in res.detail
    assert "acceptée" not in res.detail


def test_web_variant_skips_key_opencode_and_network(monkeypatch):
    monkeypatch.delenv("MISTRAL_API_KEY")
    res = lc.check_llm_cloud_web(_cloud_cfg(), **_probes(
        opencode_config_reader=_boom, auth_reader=_boom, db_reader=_boom, catalog_probe=_boom, provider_probe=_boom))
    assert res.status == doc.OK
    assert res.name == "LLM d'arbitrage cloud (validation, tier web)"


def test_web_variant_still_requires_the_admin_approval():
    res = lc.check_llm_cloud_web(_cloud_cfg(), **_probes(ack_reader=lambda c, p, m: None))
    assert res.status == doc.FAIL
    assert "Administration → LLM cloud" in res.hint


# ── Fournisseur ──────────────────────────────────────────────────────────────


def test_no_provider_in_model_id_fails():
    cfg = _cloud_cfg(workflow={"arbitration_llm": {"model_id": "sans-fournisseur"},
                               "summary_llm": {"model_id": "sans-fournisseur"}})
    res = lc.check_llm_cloud(cfg, **_probes(ack_reader=_boom))
    assert res.status == doc.FAIL and "fournisseur introuvable" in res.detail


def test_local_provider_in_cloud_fails():
    cfg = _cloud_cfg(workflow={"arbitration_llm": {"model_id": "local/arbitrage"},
                               "summary_llm": {"model_id": "local/arbitrage"}})
    res = lc.check_llm_cloud(cfg, **_probes(ack_reader=_boom))
    assert res.status == doc.FAIL and "local" in res.detail


def test_two_different_model_ids_fail():
    cfg = _cloud_cfg(workflow={"summary_llm": {"model_id": "mistral/autre"}})
    res = lc.check_llm_cloud(cfg, **_probes(ack_reader=_boom))
    assert res.status == doc.FAIL and "diffèrent" in res.detail


def test_different_ids_tolerated_when_one_section_disabled():
    cfg = _cloud_cfg(workflow={"summary_llm": {"enabled": False, "model_id": "local/arbitrage"}})
    assert lc.check_llm_cloud(cfg, **_probes()).status == doc.OK


def test_unlisted_provider_without_api_base_fails():
    cfg = _cloud_cfg(workflow={"arbitration_llm": {"model_id": "maison/m1", "api_base": ""},
                               "summary_llm": {"model_id": "maison/m1"}})
    res = lc.check_llm_cloud(cfg, **_probes(ack_reader=_boom))
    assert res.status == doc.FAIL and "hors table" in res.detail and "api_base" in res.hint


def test_unlisted_provider_without_key_env_fails():
    cfg = _cloud_cfg(workflow={"arbitration_llm": {"model_id": "maison/m1", "api_base": "https://llm.example/v1"},
                               "summary_llm": {"model_id": "maison/m1"}})
    res = lc.check_llm_cloud(cfg, **_probes(ack_reader=_boom))
    assert res.status == doc.FAIL and "api_key_env" in res.hint


# ── Clé ──────────────────────────────────────────────────────────────────────


def test_missing_key_fails_and_names_the_variable(monkeypatch):
    monkeypatch.delenv("MISTRAL_API_KEY")
    res = lc.check_llm_cloud(_cloud_cfg(), **_probes(opencode_config_reader=_boom))
    assert res.status == doc.FAIL
    assert "MISTRAL_API_KEY" in res.detail and "MISTRAL_API_KEY" in res.hint


def test_malformed_key_fails_without_echoing_it(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "sk-abc\ndef")
    res = lc.check_llm_cloud(_cloud_cfg(), **_probes())
    assert res.status == doc.FAIL
    assert "sk-abc" not in res.detail and "sk-abc" not in (res.hint or "")


# ── opencode.json ────────────────────────────────────────────────────────────


def test_missing_opencode_json_fails():
    res = lc.check_llm_cloud(_cloud_cfg(), **_probes(opencode_config_reader=lambda p: None))
    assert res.status == doc.FAIL and "/x/opencode.json" in res.detail
    assert "setup_opencode.py" in res.hint


def test_share_not_disabled_fails():
    res = lc.check_llm_cloud(_cloud_cfg(), **_probes(opencode_config_reader=lambda p: {"provider": {}}))
    assert res.status == doc.FAIL and "share" in res.detail and "manual" in res.detail


def test_residual_api_key_for_listed_provider_fails():
    data = {"share": "disabled", "provider": {"mistral": {"options": {"apiKey": SECRET}}}}
    res = lc.check_llm_cloud(_cloud_cfg(), **_probes(opencode_config_reader=lambda p: data))
    assert res.status == doc.FAIL and "apiKey" in res.detail
    _assert_no_secret(res)


def test_unlisted_provider_literal_api_key_fails_env_reference_passes(monkeypatch):
    monkeypatch.setenv("MAISON_KEY", "k-maison")
    cfg = _cloud_cfg(workflow={
        "arbitration_llm": {"model_id": "maison/m1", "api_base": "https://llm.example/v1"},
        "summary_llm": {"model_id": "maison/m1"},
        "llm_cloud": {"api_key_env": "MAISON_KEY"}})
    literal = {"share": "disabled", "provider": {"maison": {"options": {"apiKey": "k-maison"}}}}
    env_ref = {"share": "disabled", "provider": {"maison": {"options": {"apiKey": "{env:MAISON_KEY}"}}}}
    ok_probes = {"catalog_probe": lambda c: ["maison/m1"], "provider_probe": lambda c: (["m1"], None)}
    res = lc.check_llm_cloud(cfg, **_probes(opencode_config_reader=lambda p: literal, **ok_probes))
    assert res.status == doc.FAIL and "EN CLAIR" in res.detail and "{env:VAR}" in res.detail
    assert "k-maison" not in res.detail
    res = lc.check_llm_cloud(cfg, **_probes(opencode_config_reader=lambda p: env_ref, **ok_probes))
    assert res.status == doc.OK, res


# ── Identifiant stocké (auth.json v1) ────────────────────────────────────────


def test_stored_credential_is_warn_and_chain_continues():
    res = lc.check_llm_cloud(_cloud_cfg(), **_probes(auth_reader=lambda p: {"mistral": {"type": "api", "key": SECRET}}))
    assert res.status == doc.WARN
    assert "/x/auth.json" in res.detail and "opencode auth logout" in res.hint
    _assert_no_secret(res)


def test_stored_credential_for_other_provider_is_ignored():
    assert lc.check_llm_cloud(_cloud_cfg(), **_probes(auth_reader=lambda p: {"openrouter": {}})).status == doc.OK


def test_v2_stored_credential_is_warn():
    res = lc.check_llm_cloud(_cloud_cfg(), **_probes(db_reader=lambda p: {"mistral", "openrouter"}))
    assert res.status == doc.WARN
    assert "/x/opencode.db" in res.detail and "opencode auth logout" in res.hint


def test_v2_credential_for_other_provider_is_ignored():
    assert lc.check_llm_cloud(_cloud_cfg(), **_probes(db_reader=lambda p: {"openrouter"})).status == doc.OK


def test_v2_unreadable_db_is_warn_not_crash():
    def _locked(path):
        raise sqlite3.OperationalError("database is locked")

    res = lc.check_llm_cloud(_cloud_cfg(), **_probes(db_reader=_locked))
    assert res.status == doc.WARN and "non vérifiés" in res.detail


def test_default_v2_reader_reads_only_provider_ids(tmp_path):
    db = tmp_path / "opencode.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE credential (id text PRIMARY KEY, integration_id text, label text NOT NULL, "
                 "value text NOT NULL, active integer)")
    conn.execute("INSERT INTO credential VALUES ('c1', 'mistral', 'default', ?, 1)",
                 (f'{{"type":"key","key":"{SECRET}"}}',))
    conn.execute("INSERT INTO credential VALUES ('c2', NULL, 'x', '{}', 0)")
    conn.commit()
    conn.close()
    assert lc._read_v2_credential_providers(str(db)) == {"mistral"}
    assert lc._read_v2_credential_providers(str(tmp_path / "absente.db")) is None
    res = lc.check_llm_cloud(_cloud_cfg(), **_probes(db_path=str(db), db_reader=lc._read_v2_credential_providers))
    assert res.status == doc.WARN
    _assert_no_secret(res)


# ── Catalogue opencode ───────────────────────────────────────────────────────


def test_catalog_probe_failure_is_warn():
    def _mute(cfg):
        raise TimeoutError("15 s")

    res = lc.check_llm_cloud(_cloud_cfg(), **_probes(catalog_probe=_mute))
    assert res.status == doc.WARN and "catalogue opencode illisible" in res.detail


def test_model_absent_from_catalog_fails():
    res = lc.check_llm_cloud(_cloud_cfg(), **_probes(catalog_probe=lambda c: ["mistral/autre"], provider_probe=_boom))
    assert res.status == doc.FAIL and "Model unavailable" in res.detail
    assert "opencode models mistral" in res.hint


class _FakePopen:
    """Doublure de ``subprocess.Popen`` : rejoue une sortie par commande, ou un délai dépassé."""

    calls: list = []

    def __init__(self, cmd, **kwargs):
        self.cmd, self.kwargs = cmd, kwargs
        self.pid = 4242
        self.returncode = 0
        _FakePopen.calls.append(self)

    def communicate(self, timeout=None):
        if self.cmd[1:] == ["--version"]:
            return _FakePopen.version, ""
        if _FakePopen.hang and not getattr(self, "killed", False):
            raise subprocess.TimeoutExpired(self.cmd, timeout)
        return "local/arbitrage\nmistral/mistral-large-4\n\n", ""

    def send_signal(self, sig):  # pragma: no cover — repli si killpg échoue
        self.killed = True


def _install_fake_popen(monkeypatch, *, version: str, hang: bool = False) -> list:
    _FakePopen.calls, _FakePopen.version, _FakePopen.hang = [], version, hang
    monkeypatch.setattr(lc, "find_opencode_binary", lambda config_bin=None: "/bin/opencode")
    monkeypatch.setattr(lc.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(lc.subprocess, "run", _boom)  # plus aucun subprocess.run hors groupe
    return _FakePopen.calls


def test_default_catalog_probe_v1_command(monkeypatch):
    calls = _install_fake_popen(monkeypatch, version="1.18.30\n")
    assert lc._probe_opencode_catalog(_cloud_cfg()) == ["local/arbitrage", MODEL_ID]
    models = calls[-1]
    assert models.cmd == ["/bin/opencode", "models"]
    assert "transcria_doctor_catalog_" in models.kwargs["env"]["XDG_DATA_HOME"]  # data home isolé
    assert all(c.kwargs["start_new_session"] for c in calls)


def test_default_catalog_probe_v2_is_standalone(monkeypatch):
    calls = _install_fake_popen(monkeypatch, version="opencode v2.2.5\n")
    assert lc._probe_opencode_catalog(_cloud_cfg()) == ["local/arbitrage", MODEL_ID]
    assert calls[-1].cmd == ["/bin/opencode", "models", "--standalone"]  # jamais le service partagé
    assert calls[-1].kwargs["start_new_session"] is True


def test_default_catalog_probe_timeout_kills_the_process_group(monkeypatch):
    calls = _install_fake_popen(monkeypatch, version="opencode v2.2.5\n", hang=True)
    killed = []

    def fake_killpg(pgid, sig):
        killed.append((pgid, sig))
        calls[-1].killed = True

    monkeypatch.setattr(lc.os, "getpgid", lambda pid: pid + 1)
    monkeypatch.setattr(lc.os, "killpg", fake_killpg)
    with pytest.raises(TimeoutError):
        lc._probe_opencode_catalog(_cloud_cfg())
    assert killed == [(4243, signal.SIGTERM)]  # le GROUPE, pas le seul PID


# ── Fournisseur (réseau) ─────────────────────────────────────────────────────


@pytest.mark.parametrize("status", [401, 403])
def test_provider_rejects_key_fails(status):
    probe = lambda cfg: (None, f"statut HTTP {status}: Unauthorized")  # noqa: E731
    res = lc.check_llm_cloud(_cloud_cfg(), **_probes(provider_probe=probe, ack_reader=_boom))
    assert res.status == doc.FAIL and "refuse la clé" in res.detail


def test_provider_unreachable_is_warn():
    probe = lambda cfg: (None, "résolution DNS impossible: api.mistral.ai")  # noqa: E731
    res = lc.check_llm_cloud(_cloud_cfg(), **_probes(provider_probe=probe))
    assert res.status == doc.WARN and "injoignable" in res.detail
    assert "HTTPS_PROXY" in res.hint


def test_provider_other_http_error_is_warn():
    res = lc.check_llm_cloud(_cloud_cfg(), **_probes(provider_probe=lambda c: (None, "statut HTTP 503: busy")))
    assert res.status == doc.WARN and "réponse inattendue" in res.detail


def test_model_not_served_by_provider_fails():
    res = lc.check_llm_cloud(_cloud_cfg(), **_probes(provider_probe=lambda c: (["autre"], None), ack_reader=_boom))
    assert res.status == doc.FAIL and "absent des 1 modèles" in res.detail


def test_empty_provider_list_is_not_conclusive():
    assert lc.check_llm_cloud(_cloud_cfg(), **_probes(provider_probe=lambda c: ([], None))).status == doc.OK


def test_default_provider_probe_uses_bearer_and_never_leaks_key(monkeypatch):
    calls = {}

    class _Resp:
        status_code = 401
        text = "invalid api key"

    def fake_get(url, timeout=None, headers=None):
        calls["url"], calls["headers"] = url, headers
        return _Resp()

    import requests

    monkeypatch.setattr(requests, "get", fake_get)
    res = lc.check_llm_cloud(_cloud_cfg(), **_probes(provider_probe=lc._probe_provider_models, ack_reader=_boom))
    assert calls["url"] == "https://api.mistral.ai/v1/models"
    assert calls["headers"] == {"Authorization": f"Bearer {SECRET}"}
    assert res.status == doc.FAIL
    _assert_no_secret(res)


def test_401_body_with_partially_masked_key_is_not_reported():
    probe = lambda cfg: (None, "statut HTTP 401: Incorrect API key provided: sk-ab...wxyz")  # noqa: E731
    res = lc.check_llm_cloud(_cloud_cfg(), **_probes(provider_probe=probe, ack_reader=_boom))
    assert res.status == doc.FAIL and "statut HTTP 401" in res.detail
    assert "sk-ab" not in res.detail and "wxyz" not in res.detail and "Incorrect" not in res.detail


def test_key_like_patterns_are_scrubbed_even_if_not_the_configured_key():
    probe = lambda cfg: (None, "statut HTTP 502: echo sk-ab…wxyz12 / Authorization: Bearer abc.DEF-123")  # noqa: E731
    res = lc.check_llm_cloud(_cloud_cfg(), **_probes(provider_probe=probe))
    assert res.status == doc.WARN
    assert "sk-ab" not in res.detail and "abc.DEF-123" not in res.detail
    assert "Bearer ***" in res.detail


def test_secret_echoed_by_a_probe_is_masked():
    probe = lambda cfg: (None, f"connexion refusée/coupée: {SECRET}")  # noqa: E731
    res = lc.check_llm_cloud(_cloud_cfg(), **_probes(provider_probe=probe))
    assert res.status == doc.WARN
    _assert_no_secret(res)
    assert "***" in res.detail


# ── Validation administrateur ────────────────────────────────────────────────


def test_missing_ack_fails():
    res = lc.check_llm_cloud(_cloud_cfg(), **_probes(ack_reader=lambda c, p, m: None))
    assert res.status == doc.FAIL and "aucune validation" in res.detail
    assert "Administration → LLM cloud" in res.hint


def test_stale_ack_fails():
    stale = lambda c, p, m: {"provider": p, "model": m, "text_version": "ancien", "revoked_at": None}  # noqa: E731
    res = lc.check_llm_cloud(_cloud_cfg(), **_probes(ack_reader=stale))
    assert res.status == doc.FAIL and "caduque" in res.detail and CLOUD_NOTICE_TEXT_VERSION in res.detail


def test_unreadable_ack_fails():
    def _down(cfg, provider, model):
        raise RuntimeError("table absente")

    res = lc.check_llm_cloud(_cloud_cfg(), **_probes(ack_reader=_down))
    assert res.status == doc.FAIL and "illisible" in res.detail


def test_default_ack_reader_reads_latest_non_revoked_row(tmp_path, monkeypatch):
    # Un run_doctor d'un autre test peut avoir chargé un .env : l'URL de config doit primer ici.
    monkeypatch.delenv("TRANSCRIA_DATABASE_URL", raising=False)
    from sqlalchemy import create_engine, text

    uri = f"sqlite:///{tmp_path / 'acks.db'}"
    engine = create_engine(uri)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE llm_cloud_acknowledgements (provider TEXT, model TEXT, "
                          "text_version TEXT, revoked_at TEXT, acknowledged_at TEXT)"))
        conn.execute(text("INSERT INTO llm_cloud_acknowledgements VALUES "
                          "('mistral','mistral-large-4','v-old',NULL,'2026-10-01'),"
                          "('mistral','mistral-large-4',:v,NULL,'2026-11-01'),"
                          "('mistral','mistral-large-4','v-revoked','2026-11-02','2026-11-02')"),
                     {"v": CLOUD_NOTICE_TEXT_VERSION})
    engine.dispose()
    cfg = _cloud_cfg(storage={"database_url": uri})
    row = lc._read_cloud_ack(cfg, "Mistral", "mistral-large-4")
    assert row is not None and row["text_version"] == CLOUD_NOTICE_TEXT_VERSION
    assert lc._read_cloud_ack(cfg, "mistral", "autre") is None


# ── Checks LOCAUX neutralisés en cloud ───────────────────────────────────────


def test_local_llm_checks_are_not_applicable_in_cloud():
    cfg = _cloud_cfg(services={"arbitrage_script": "/nope.sh"}, gpu={"llm_gpu_indices": [0]})
    for res in (
        doc.check_arbitrage_script(cfg, is_file=_boom),
        doc.check_llm_placement_declaration(cfg, is_file=_boom),
        doc.check_arbitrage_llm(cfg, probe=_boom),
    ):
        assert res.status == doc.OK and "sans objet" in res.detail


def test_local_llm_checks_unchanged_outside_cloud():
    cfg = make_config({"services": {"arbitrage_script": "/nope.sh"}})
    assert doc.check_arbitrage_script(cfg, is_file=lambda p: False).status == doc.FAIL
    assert doc.check_arbitrage_llm(cfg, probe=lambda port: None).status == doc.WARN


def test_model_resolution_listed_provider_needs_no_block():
    res = doc.check_opencode_model_resolution(_cloud_cfg(), reader=_boom)
    assert res.status == doc.OK and "catalogue opencode" in res.detail


def test_model_resolution_unlisted_provider_requires_block():
    cfg = _cloud_cfg(workflow={"arbitration_llm": {"model_id": "maison/m1", "api_base": "https://llm.example/v1"},
                               "summary_llm": {"model_id": "maison/m1"}})
    res = doc.check_opencode_model_resolution(cfg, config_path="/x/oc.json", reader=lambda p: {"provider": {}})
    assert res.status == doc.FAIL and "setup_opencode.py" in res.hint
    block = {"provider": {"maison": {"models": {"m1": {}}}}}
    res = doc.check_opencode_model_resolution(cfg, config_path="/x/oc.json", reader=lambda p: block)
    assert res.status == doc.OK


# ── --llm-smoke en cloud ─────────────────────────────────────────────────────


def _factory(result):
    class _Runner:
        def __init__(self, work_dir, config=None):
            pass

        def run(self, instruction, prompt_file, timeout=600):
            return result

    return lambda work_dir, config=None: _Runner(work_dir, config)


def test_smoke_cloud_announces_paid_call_before_running():
    order = []

    class _Runner:
        def __init__(self, work_dir, config=None):
            pass

        def run(self, instruction, prompt_file, timeout=600):
            order.append("run")
            return {"success": True, "output": "OK"}

    res = doc.check_opencode_smoke(_cloud_cfg(), runner_factory=lambda w, config=None: _Runner(w, config),
                                   probe=_boom, notify=lambda msg: order.append(msg))
    assert res.status == doc.OK
    assert order[0].startswith("APPEL PAYANT") and order[1] == "run"
    assert "APPEL PAYANT" in res.detail and "mistral" in res.detail


def test_smoke_cloud_failure_keeps_the_paid_notice():
    res = doc.check_opencode_smoke(_cloud_cfg(), runner_factory=_factory({"success": False, "error": "x"}),
                                   notify=lambda m: None)
    assert res.status == doc.FAIL and "APPEL PAYANT" in res.detail


def test_smoke_local_never_notifies():
    cfg = make_config({"workflow": {"arbitration_llm": {"enabled": True}}})
    res = doc.check_opencode_smoke(cfg, runner_factory=_factory({"success": True, "output": "OK"}),
                                   probe=lambda port: {"data": [{"id": "x"}]}, notify=_boom)
    assert res.status == doc.OK and "port" in res.detail


# ── Registre ─────────────────────────────────────────────────────────────────


def test_registry_profiles():
    assert lc.check_llm_cloud in doc._PROFILE_CHECKS["scheduler"]
    assert lc.check_llm_cloud in doc._PROFILE_CHECKS["all-in-one"]
    assert lc.check_llm_cloud_web in doc._PROFILE_CHECKS["web"]
    assert lc.check_llm_cloud not in doc._PROFILE_CHECKS["web"]


def test_english_catalog_has_every_cloud_key():
    from transcria.diagnostics.doctor_messages import DOCTOR_MESSAGES

    fr_keys = {k for k in DOCTOR_MESSAGES["fr"] if k.startswith(("lc_", "mr_cloud_", "smoke_cloud_", "chk_llm_cloud"))}
    fr_keys |= {"na_cloud", "chk_model_resolution_cloud"}
    assert fr_keys <= set(DOCTOR_MESSAGES["en"])

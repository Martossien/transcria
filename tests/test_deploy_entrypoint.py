"""Tests de l'entrypoint Docker par rôle (`transcria.deploy.entrypoint`).

Sonde DB et exec sont injectés : on vérifie les commandes par rôle, les gardes
(config absente, PostgreSQL obligatoire, SQLite refusé), l'attente de base et le
remplacement de process — sans conteneur, sans base, sans serveur réel.
"""
from __future__ import annotations

from pathlib import Path

from transcria.deploy import entrypoint as ep


def _config(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text("server:\n  port: 7870\n", encoding="utf-8")
    return p


def _plan(tmp_path: Path, role: str, **kw) -> ep.EntrypointPlan:
    defaults = dict(role=role, config_path=_config(tmp_path), database_url="postgresql+psycopg://u:p@db:5432/x")
    defaults.update(kw)
    return ep.EntrypointPlan(**defaults)


# ── Commandes par rôle ──────────────────────────────────────────────────────


def test_all_command_runs_app_role_all_with_host_port(tmp_path):
    cmd = ep.build_role_command(_plan(tmp_path, "all", bind="0.0.0.0:7870"))
    assert cmd[1:] == ["app.py", "--role", "all", "--host", "0.0.0.0", "--port", "7870"]


def test_all_requires_postgres(tmp_path):
    errors = ep.preflight(_plan(tmp_path, "all", database_url=""))
    assert any("TRANSCRIA_DATABASE_URL requis" in e for e in errors)


def test_all_preflight_ok_with_postgres(tmp_path):
    assert ep.preflight(_plan(tmp_path, "all")) == []


def test_web_command_is_gunicorn_wsgi(tmp_path):
    cmd = ep.build_role_command(_plan(tmp_path, "web", workers=4, bind="0.0.0.0:7870"))
    assert cmd[0] == "gunicorn" and cmd[-1] == "wsgi:app"
    assert "--workers" in cmd and "4" in cmd
    assert "0.0.0.0:7870" in cmd


def test_scheduler_command_runs_app_with_role(tmp_path):
    cmd = ep.build_role_command(_plan(tmp_path, "scheduler"))
    assert cmd[1:] == ["app.py", "--role", "scheduler"]


def test_resource_node_command_is_inference_gunicorn(tmp_path):
    cmd = ep.build_role_command(_plan(tmp_path, "resource-node", inference_bind="0.0.0.0:8002"))
    assert "inference_service:create_app()" in cmd
    assert "0.0.0.0:8002" in cmd


def test_migrate_command_is_alembic_upgrade(tmp_path):
    cmd = ep.build_role_command(_plan(tmp_path, "migrate"))
    assert cmd == ["alembic", "upgrade", "head"]


# ── Préflight (gardes invariants conteneur) ─────────────────────────────────


def test_preflight_ok_for_valid_web(tmp_path):
    assert ep.preflight(_plan(tmp_path, "web")) == []


def test_preflight_missing_config_is_error(tmp_path):
    plan = ep.EntrypointPlan(role="web", config_path=tmp_path / "absent.yaml",
                             database_url="postgresql+psycopg://u:p@db/x")
    errors = ep.preflight(plan)
    assert any("config.yaml introuvable" in e for e in errors)


def test_preflight_requires_postgres_for_db_roles(tmp_path):
    errors = ep.preflight(_plan(tmp_path, "scheduler", database_url=""))
    assert any("TRANSCRIA_DATABASE_URL requis" in e for e in errors)


def test_preflight_rejects_sqlite_in_container(tmp_path):
    errors = ep.preflight(_plan(tmp_path, "web", database_url="sqlite:///x.db"))
    assert any("SQLite refusé" in e for e in errors)


def test_preflight_resource_node_does_not_require_db(tmp_path):
    # nœud GPU pur : pas de base applicative exigée
    assert ep.preflight(_plan(tmp_path, "resource-node", database_url="")) == []


# ── Attente DB ──────────────────────────────────────────────────────────────


def test_wait_for_database_succeeds_after_retries():
    calls = {"n": 0}

    def probe(_dsn):
        calls["n"] += 1
        return calls["n"] >= 3  # joignable à la 3e tentative

    assert ep.wait_for_database("dsn", probe=probe, attempts=5, delay=0, sleep_fn=lambda _d: None)
    assert calls["n"] == 3


def test_wait_for_database_gives_up():
    assert ep.wait_for_database("dsn", probe=lambda _d: False, attempts=4, delay=0, sleep_fn=lambda _d: None) is False


# ── classify_db_unreachable : message actionnable (auth vs réseau) ───────────


def _diag_with_error(monkeypatch, exc):
    # Simule l'échec de connexion SQLAlchemy avec une exception donnée.
    import transcria.deploy.entrypoint as mod

    class _Engine:
        def connect(self):
            raise exc

        def dispose(self):
            pass

    monkeypatch.setattr(mod, "create_engine", lambda url: _Engine(), raising=False)
    import sqlalchemy
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda url: _Engine())
    return mod.classify_db_unreachable("postgresql+psycopg://u:p@db/x")


def test_classify_auth_failure_explains_stale_volume(monkeypatch):
    msg = _diag_with_error(monkeypatch, Exception('FATAL: password authentication failed for user "transcria"'))
    assert "AUTHENTIFICATION" in msg
    assert "POSTGRES_PASSWORD" in msg and "down -v" in msg  # piège volume + remédiation


def test_classify_dns_failure(monkeypatch):
    msg = _diag_with_error(monkeypatch, Exception("could not translate host name \"db\" to address"))
    assert "DNS" in msg or "HÔTE" in msg


def test_classify_connection_refused(monkeypatch):
    msg = _diag_with_error(monkeypatch, Exception("connection refused"))
    assert "REFUSÉE" in msg


def test_main_migrate_failure_reports_actionable_cause(tmp_path):
    # La cause classée doit remonter dans le message d'erreur du rôle (pas un « injoignable » sec).
    env = {"TRANSCRIA_ROLE": "migrate", "TRANSCRIA_CONFIG": str(_config(tmp_path)),
           "TRANSCRIA_DATABASE_URL": "postgresql+psycopg://u:p@db/x"}
    captured: dict = {}
    import io
    import sys as _sys
    buf = io.StringIO()
    old = _sys.stderr
    _sys.stderr = buf
    try:
        rc = ep.main(["migrate"], env=env, exec_fn=_Exec(), db_probe=lambda _d: False,
                     db_diagnoser=lambda _url: "AUTHENTIFICATION refusée (volume préexistant)",
                     wait_attempts=2, wait_delay=0, sleep_fn=lambda _d: None,
                     opencode_provisioner=lambda _p, _e: None)
    finally:
        _sys.stderr = old
        captured["err"] = buf.getvalue()
    assert rc == 1
    assert "AUTHENTIFICATION refusée" in captured["err"]
    assert "inaccessible" in captured["err"]


# ── main : orchestration + exec ─────────────────────────────────────────────


class _Exec:
    def __init__(self):
        self.calls: list[tuple[str, list[str]]] = []

    def __call__(self, file, args):
        self.calls.append((file, list(args)))


def test_main_execs_web_after_db_ready(tmp_path):
    env = {"TRANSCRIA_CONFIG": str(_config(tmp_path)), "TRANSCRIA_DATABASE_URL": "postgresql+psycopg://u:p@db/x"}
    ex = _Exec()
    rc = ep.main(["web"], env=env, exec_fn=ex, db_probe=lambda _d: True)
    assert rc == 0
    assert ex.calls and ex.calls[0][0] == "gunicorn"
    assert ex.calls[0][1][-1] == "wsgi:app"


def test_main_role_from_env(tmp_path):
    env = {"TRANSCRIA_ROLE": "scheduler", "TRANSCRIA_CONFIG": str(_config(tmp_path)),
           "TRANSCRIA_DATABASE_URL": "postgresql+psycopg://u:p@db/x"}
    ex = _Exec()
    # provisioner injecté (no-op) : évite d'écrire dans le ~/.config réel pendant les tests.
    rc = ep.main([], env=env, exec_fn=ex, db_probe=lambda _d: True,
                 opencode_provisioner=lambda _plan, _env: None)
    assert rc == 0
    assert ex.calls[0][1][1:] == ["app.py", "--role", "scheduler"]  # [0] = interpréteur


# ── Provisioning opencode (provider local) au démarrage ─────────────────────


def test_main_invokes_opencode_provisioner_for_llm_role(tmp_path):
    env = {"TRANSCRIA_CONFIG": str(_config(tmp_path)), "TRANSCRIA_DATABASE_URL": "postgresql+psycopg://u:p@db/x"}
    seen = []
    ep.main(["scheduler"], env=env, exec_fn=_Exec(), db_probe=lambda _d: True,
            opencode_provisioner=lambda plan, _env: seen.append(plan.role))
    assert seen == ["scheduler"]  # appelé avant l'exec, pour un rôle LLM


def test_main_invokes_provisioner_for_web_but_it_self_skips(tmp_path):
    # main() appelle toujours le provisioner ; c'est provision_opencode qui filtre par rôle.
    env = {"TRANSCRIA_CONFIG": str(_config(tmp_path)), "TRANSCRIA_DATABASE_URL": "postgresql+psycopg://u:p@db/x"}
    seen = []
    ep.main(["web"], env=env, exec_fn=_Exec(), db_probe=lambda _d: True,
            opencode_provisioner=lambda plan, _env: seen.append(plan.role))
    assert seen == ["web"]


def test_provision_opencode_skips_non_llm_roles(tmp_path):
    # resource-node / web / migrate : aucun fichier opencode.json écrit (retour anticipé).
    target = tmp_path / "oc.json"
    for role in ("web", "resource-node", "migrate"):
        ep.provision_opencode(_plan(tmp_path, role), {"OPENCODE_CONFIG": str(target)})
        assert not target.exists()


def test_provision_opencode_writes_provider_from_mounted_config(tmp_path, monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        "transcria.config.load_config",
        lambda: {"services": {"arbitrage_llm_host": "vllm-arbitrage", "arbitrage_llm_port": 8080}},
    )
    monkeypatch.setattr(
        "transcria.gpu.opencode_setup.ensure_local_provider",
        lambda path, base_url, model, **kw: captured.update(path=str(path), base_url=base_url, model=model),
    )
    oc = tmp_path / "oc.json"
    ep.provision_opencode(_plan(tmp_path, "scheduler"), {"OPENCODE_CONFIG": str(oc)})
    assert captured["base_url"] == "http://vllm-arbitrage:8080/v1"
    assert captured["model"] == "arbitrage"
    assert captured["path"].endswith("oc.json")
    # La politique headless `external_directory` doit aussi être écrite (correctif du blocage
    # `ask` qui suspendait `opencode run` en split). ensure_local_provider est mocké ; c'est
    # ensure_agent_permissions (réel) qui écrit oc.json ici.
    import json as _json

    from transcria.workflow.agent_workspace import resolve_agent_work_root
    perm = _json.loads(oc.read_text())["permission"]["external_directory"]
    assert perm == {f"{resolve_agent_work_root({})}/**": "allow", "*": "deny"}


def test_provision_opencode_is_best_effort_on_error(tmp_path, monkeypatch):
    def _boom() -> dict:
        raise RuntimeError("config illisible")

    monkeypatch.setattr("transcria.config.load_config", _boom)
    # Ne doit PAS lever : un échec de provisioning ne bloque jamais le démarrage du rôle.
    ep.provision_opencode(_plan(tmp_path, "all"), {"OPENCODE_CONFIG": str(tmp_path / "oc.json")})


def test_main_refuses_without_postgres(tmp_path):
    env = {"TRANSCRIA_CONFIG": str(_config(tmp_path))}  # pas de DSN
    ex = _Exec()
    rc = ep.main(["web"], env=env, exec_fn=ex, db_probe=lambda _d: True)
    assert rc == 1
    assert ex.calls == []  # rien exécuté


def test_main_fails_when_db_never_ready(tmp_path):
    env = {"TRANSCRIA_CONFIG": str(_config(tmp_path)), "TRANSCRIA_DATABASE_URL": "postgresql+psycopg://u:p@db/x"}
    ex = _Exec()
    rc = ep.main(["migrate"], env=env, exec_fn=ex, db_probe=lambda _d: False,
                 wait_attempts=2, wait_delay=0, sleep_fn=lambda _d: None)
    assert rc == 1
    assert ex.calls == []


def test_main_resource_node_skips_db_wait_and_execs(tmp_path):
    env = {"TRANSCRIA_CONFIG": str(_config(tmp_path))}  # pas de DSN → OK pour resource-node
    ex = _Exec()
    probe_calls = {"n": 0}
    rc = ep.main(["resource-node"], env=env, exec_fn=ex,
                 db_probe=lambda _d: probe_calls.__setitem__("n", probe_calls["n"] + 1) or True)
    assert rc == 0
    assert probe_calls["n"] == 0  # nœud GPU : pas d'attente DB
    assert "inference_service:create_app()" in ex.calls[0][1]

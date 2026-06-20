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
    rc = ep.main([], env=env, exec_fn=ex, db_probe=lambda _d: True)
    assert rc == 0
    assert ex.calls[0][1][1:] == ["app.py", "--role", "scheduler"]  # [0] = interpréteur


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

"""Entrypoint Docker par rôle : `python -m transcria.deploy.entrypoint <role>`.

Invariants P5 (cf. docs/PLAN_EVOLUTION_INSTALLATION.md § P5) :
  * mêmes profils que l'install (`web`, `scheduler`, `resource-node`, `migrate`) ;
  * `install.sh` n'est JAMAIS l'entrypoint applicatif — l'image est déjà construite,
    le runtime ne fait que provisionner puis exec la commande du rôle ;
  * PostgreSQL obligatoire (SQLite refusé) pour les rôles à base applicative ;
  * `migrate` est un job one-shot (`alembic upgrade head`) ; les rôles serveurs
    attendent que la base soit joignable puis démarrent (pas de migration implicite —
    elle est jouée par le job `migrate` dédié).

Le process est **remplacé** (`os.execvp`) pour que le serveur hérite de PID 1 et
reçoive proprement les signaux du conteneur. Sonde DB et exec sont injectables (tests).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

# Rôles conteneurisables et ceux qui exigent une base applicative PostgreSQL.
#   all          : tout-en-un (UI web + scheduler + inférence GPU in-process) — le plus
#                  simple pour tester le projet dans un seul conteneur ;
#   web/scheduler: déploiement distribué (split) ;
#   resource-node: nœud GPU pur (STT/diarisation), sans base applicative ;
#   migrate      : job one-shot Alembic.
ROLES = ("all", "web", "scheduler", "resource-node", "migrate")
_DB_ROLES = ("all", "web", "scheduler", "migrate")  # resource-node = nœud GPU pur, sans base applicative

ExecFn = Callable[[str, Sequence[str]], None]
DbProbe = Callable[[str], bool]


@dataclass(frozen=True)
class EntrypointPlan:
    role: str
    config_path: Path
    database_url: str = ""
    bind: str = "0.0.0.0:7870"
    inference_bind: str = "0.0.0.0:8002"
    workers: int = 4
    threads: int = 4
    timeout: int = 120
    python: str = field(default_factory=lambda: sys.executable)
    gunicorn: str = "gunicorn"
    alembic: str = "alembic"
    app_module: str = "app.py"


def needs_database(role: str) -> bool:
    return role in _DB_ROLES


def _split_bind(bind: str) -> tuple[str, str]:
    """'host:port' → (host, port). Tolère un bind sans port."""
    host, _, port = bind.rpartition(":")
    return (host or "0.0.0.0", port or "7870")


def build_role_command(plan: EntrypointPlan) -> list[str]:
    """Commande de lancement du rôle (fidèle aux unités systemd `deploy/`)."""
    if plan.role == "all":
        # Tout-en-un : app.py rôle 'all' (UI + scheduler + inférence in-process). Adapté
        # à un test mono-conteneur ; un GPU est requis pour le traitement réel des jobs.
        host, port = _split_bind(plan.bind)
        return [plan.python, plan.app_module, "--role", "all", "--host", host, "--port", port]
    if plan.role == "web":
        return [
            plan.gunicorn,
            "--workers", str(plan.workers),
            "--bind", plan.bind,
            "--timeout", str(plan.timeout),
            "--access-logfile", "-",
            "--error-logfile", "-",
            "wsgi:app",
        ]
    if plan.role == "scheduler":
        return [plan.python, plan.app_module, "--role", "scheduler"]
    if plan.role == "resource-node":
        return [
            plan.gunicorn,
            "inference_service:create_app()",
            "-b", plan.inference_bind,
            "--workers", "1",
            "--threads", str(plan.threads),
            "--timeout", str(plan.timeout),
            "--access-logfile", "-",
            "--error-logfile", "-",
        ]
    if plan.role == "migrate":
        return [plan.alembic, "upgrade", "head"]
    raise ValueError(f"rôle inconnu : {plan.role}")


def preflight(plan: EntrypointPlan) -> list[str]:
    """Vérifie les invariants conteneur. Retourne la liste des erreurs actionnables."""
    errors: list[str] = []
    if plan.role not in ROLES:
        errors.append(f"rôle inconnu '{plan.role}' (attendus : {', '.join(ROLES)})")
        return errors

    if not plan.config_path.is_file():
        errors.append(
            f"config.yaml introuvable ({plan.config_path}) — monter le volume de configuration "
            "(ex. -v ./config.yaml:/app/config.yaml) ou définir TRANSCRIA_CONFIG."
        )

    if needs_database(plan.role):
        dsn = plan.database_url.strip()
        if not dsn:
            errors.append(
                "TRANSCRIA_DATABASE_URL requis : PostgreSQL est obligatoire en conteneur "
                f"pour le rôle '{plan.role}' (SQLite n'est pas un mode de déploiement Docker supporté)."
            )
        elif dsn.startswith("sqlite"):
            errors.append(
                f"SQLite refusé en conteneur (rôle '{plan.role}') : fournir un DSN PostgreSQL "
                "dans TRANSCRIA_DATABASE_URL."
            )
    return errors


def _default_exec(file: str, args: Sequence[str]) -> None:
    os.execvp(file, list(args))  # remplace le process (PID 1 hérite des signaux)


def _default_db_probe(database_url: str) -> bool:
    # Réutilise la sonde de la phase PostgreSQL (SQLAlchemy/psycopg, sans dépendre de psql).
    from transcria.installer.postgres_phase import _default_query

    return _default_query(database_url, "SELECT 1") == "1"


def wait_for_database(
    database_url: str,
    *,
    probe: DbProbe,
    attempts: int = 30,
    delay: float = 1.0,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> bool:
    """Attend que la base réponde (compose : la DB peut démarrer après le service)."""
    for attempt in range(1, attempts + 1):
        if probe(database_url):
            return True
        if attempt < attempts:
            sleep_fn(delay)
    return False


def _plan_from_env(role: str, env: dict[str, str]) -> EntrypointPlan:
    config_path = Path(env.get("TRANSCRIA_CONFIG", "/app/config.yaml"))
    return EntrypointPlan(
        role=role,
        config_path=config_path,
        database_url=env.get("TRANSCRIA_DATABASE_URL", ""),
        bind=env.get("TRANSCRIA_BIND", "0.0.0.0:7870"),
        inference_bind=env.get("INFERENCE_BIND", f"0.0.0.0:{env.get('INFERENCE_PORT', '8002')}"),
        workers=int(env.get("TRANSCRIA_WORKERS", "4")),
        threads=int(env.get("INFERENCE_THREADS", "4")),
    )


def main(
    argv: list[str] | None = None,
    *,
    env: dict[str, str] | None = None,
    exec_fn: ExecFn = _default_exec,
    db_probe: DbProbe | None = None,
    wait_attempts: int = 30,
    wait_delay: float = 1.0,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> int:
    env = dict(os.environ if env is None else env)
    probe = db_probe or _default_db_probe

    parser = argparse.ArgumentParser(
        prog="transcria-entrypoint",
        description="Entrypoint Docker par rôle (réutilise les profils d'install, sans install.sh).",
    )
    parser.add_argument("role", nargs="?", default=env.get("TRANSCRIA_ROLE"), choices=ROLES,
                        help="Rôle du conteneur (défaut : $TRANSCRIA_ROLE)")
    parser.add_argument("--no-wait-db", action="store_true", help="Ne pas attendre la base (debug)")
    args = parser.parse_args(argv)

    if not args.role:
        print("[ERROR] rôle requis : argument positionnel ou $TRANSCRIA_ROLE", file=sys.stderr)
        return 2

    plan = _plan_from_env(args.role, env)

    errors = preflight(plan)
    if errors:
        for err in errors:
            print(f"[ERROR] {err}", file=sys.stderr)
        return 1

    if needs_database(plan.role) and not args.no_wait_db:
        print(f"[INFO] Attente de la base PostgreSQL (rôle {plan.role})…", file=sys.stderr, flush=True)
        if not wait_for_database(plan.database_url, probe=probe, attempts=wait_attempts, delay=wait_delay, sleep_fn=sleep_fn):
            print(
                f"[ERROR] base PostgreSQL injoignable après {wait_attempts} tentatives — "
                "vérifier TRANSCRIA_DATABASE_URL et le service de base.",
                file=sys.stderr,
            )
            return 1

    cmd = build_role_command(plan)
    print(f"[INFO] Démarrage du rôle {plan.role} : {' '.join(cmd)}", file=sys.stderr, flush=True)
    exec_fn(cmd[0], cmd)
    return 0  # non atteint si exec réussit


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

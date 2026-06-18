from __future__ import annotations

import argparse
import json
import shlex
import sys
from dataclasses import dataclass
from typing import Any

VALID_INSTALL_PROFILES = ("all-in-one", "web", "scheduler", "resource-node", "migrate")


@dataclass(frozen=True)
class ProfileSpec:
    name: str
    runtime_role: str | None
    legacy_service: bool
    inference_service: bool
    requires_postgres: bool
    default_postgres: bool | None
    needs_local_models: bool
    needs_llm: bool
    needs_admin_config: bool
    systemd_units: tuple[str, ...]


_SPECS: dict[str, ProfileSpec] = {
    "all-in-one": ProfileSpec(
        name="all-in-one",
        runtime_role="all",
        legacy_service=True,
        inference_service=False,
        requires_postgres=False,
        default_postgres=None,
        needs_local_models=True,
        needs_llm=True,
        needs_admin_config=True,
        systemd_units=("transcria.service",),
    ),
    "web": ProfileSpec(
        name="web",
        runtime_role="web",
        legacy_service=False,
        inference_service=False,
        requires_postgres=True,
        default_postgres=True,
        needs_local_models=False,
        needs_llm=False,
        needs_admin_config=True,
        systemd_units=("transcria-migrate.service", "transcria-web.service"),
    ),
    "scheduler": ProfileSpec(
        name="scheduler",
        runtime_role="scheduler",
        legacy_service=False,
        inference_service=False,
        requires_postgres=True,
        default_postgres=True,
        needs_local_models=True,
        needs_llm=True,
        needs_admin_config=True,
        systemd_units=("transcria-migrate.service", "transcria-scheduler.service"),
    ),
    "resource-node": ProfileSpec(
        name="resource-node",
        runtime_role=None,
        legacy_service=False,
        inference_service=True,
        requires_postgres=False,
        default_postgres=False,
        needs_local_models=True,
        needs_llm=False,
        needs_admin_config=False,
        systemd_units=("transcria-inference.service",),
    ),
    "migrate": ProfileSpec(
        name="migrate",
        runtime_role=None,
        legacy_service=False,
        inference_service=False,
        requires_postgres=True,
        default_postgres=True,
        needs_local_models=False,
        needs_llm=False,
        needs_admin_config=False,
        systemd_units=("transcria-migrate.service",),
    ),
}


@dataclass(frozen=True)
class InstallPlan:
    profile: str
    runtime_role: str | None
    legacy_service: bool
    inference_service: bool
    setup_postgres: bool | None
    needs_local_models: bool
    needs_llm: bool
    needs_admin_config: bool
    systemd_units: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "runtime_role": self.runtime_role,
            "legacy_service": self.legacy_service,
            "inference_service": self.inference_service,
            "setup_postgres": self.setup_postgres,
            "needs_local_models": self.needs_local_models,
            "needs_llm": self.needs_llm,
            "needs_admin_config": self.needs_admin_config,
            "systemd_units": list(self.systemd_units),
        }


@dataclass(frozen=True)
class PlanRenderContext:
    install_dir: str
    service_user: str
    install_torch: bool = True
    postgres_host: str = "127.0.0.1"
    postgres_port: str = "5432"
    postgres_db: str = "transcria"
    postgres_user: str = "transcria"
    postgres_migrate: bool = False
    doctor_profile: str | None = None
    doctor_enabled: bool = True
    doctor_strict: bool = False


def render_install_plan_text(plan: InstallPlan, context: PlanRenderContext) -> str:
    """Rend le plan au format texte stable utilisé par `install.sh --plan`."""
    setup_postgres = "prompt" if plan.setup_postgres is None else str(plan.setup_postgres).lower()
    lines = [
        "TranscrIA install plan",
        "======================",
        f"profile={plan.profile}",
        f"runtime_role={plan.runtime_role or 'none'}",
        f"install_dir={context.install_dir}",
        f"service_user={context.service_user}",
        f"systemd={str(bool(plan.systemd_units)).lower()}",
        f"legacy_service={str(plan.legacy_service).lower()}",
        f"inference_service={str(plan.inference_service).lower()}",
        f"install_torch={str(context.install_torch).lower()}",
        f"setup_postgres={setup_postgres}",
        f"postgres_host={context.postgres_host}",
        f"postgres_port={context.postgres_port}",
        f"postgres_db={context.postgres_db}",
        f"postgres_user={context.postgres_user}",
        f"postgres_migrate={str(context.postgres_migrate).lower()}",
        f"needs_local_models={str(plan.needs_local_models).lower()}",
        f"needs_llm={str(plan.needs_llm).lower()}",
        f"needs_admin_config={str(plan.needs_admin_config).lower()}",
        f"doctor_profile={context.doctor_profile or plan.profile}",
        f"doctor_enabled={str(context.doctor_enabled).lower()}",
        f"doctor_strict={str(context.doctor_strict).lower()}",
        "",
        "systemd_units:",
    ]
    if plan.systemd_units:
        lines.extend(f"  - {unit}" for unit in plan.systemd_units)
    else:
        lines.append("  - none")
    return "\n".join(lines) + "\n"


def render_install_plan_shell(plan: InstallPlan) -> str:
    """Rend les décisions du profil en affectations shell contrôlées."""

    def value(raw: bool | str | None) -> str:
        if raw is None:
            return "''"
        if isinstance(raw, bool):
            return "true" if raw else "false"
        return shlex.quote(raw)

    assignments = {
        "INSTALL_PROFILE": plan.profile,
        "INSTALL_RUNTIME_ROLE": plan.runtime_role,
        "INSTALL_SERVICE": plan.legacy_service,
        "INSTALL_INFERENCE": plan.inference_service,
        "SETUP_PG": plan.setup_postgres,
        "PROFILE_NEEDS_LOCAL_MODELS": plan.needs_local_models,
        "PROFILE_NEEDS_LLM": plan.needs_llm,
        "PROFILE_NEEDS_ADMIN_CONFIG": plan.needs_admin_config,
    }
    return "\n".join(f"{key}={value(val)}" for key, val in assignments.items()) + "\n"


def get_profile_spec(profile: str) -> ProfileSpec:
    try:
        return _SPECS[profile]
    except KeyError as exc:
        expected = ", ".join(VALID_INSTALL_PROFILES)
        raise ValueError(f"profil inconnu: {profile} (attendus: {expected})") from exc


def resolve_install_plan(
    profile: str,
    *,
    systemd: bool = True,
    setup_postgres: bool | None = None,
) -> InstallPlan:
    """Résout les décisions d'installation dérivées du profil.

    `setup_postgres=None` signifie "non décidé par l'appelant". Les profils split
    forcent PostgreSQL ; `resource-node` force le défaut à `False` pour éviter une
    base applicative inutile sur un nœud GPU pur.
    """
    spec = get_profile_spec(profile)
    if spec.requires_postgres and setup_postgres is False:
        raise ValueError(f"--profile {profile} nécessite PostgreSQL ; SQLite dev est incompatible.")
    effective_postgres = spec.default_postgres if setup_postgres is None else setup_postgres
    units = spec.systemd_units if systemd else ()
    return InstallPlan(
        profile=spec.name,
        runtime_role=spec.runtime_role,
        legacy_service=spec.legacy_service and systemd,
        inference_service=spec.inference_service,
        setup_postgres=effective_postgres,
        needs_local_models=spec.needs_local_models,
        needs_llm=spec.needs_llm,
        needs_admin_config=spec.needs_admin_config,
        systemd_units=units,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Résout le plan d'installation TranscrIA pour un profil.")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--no-systemd", action="store_true", help="simule --no-service côté install.sh")
    parser.add_argument("--format", choices=("json", "text", "shell"), default="json")
    parser.add_argument("--install-dir", default=".")
    parser.add_argument("--service-user", default="")
    parser.add_argument("--no-torch", action="store_true")
    parser.add_argument("--pg-host", default="127.0.0.1")
    parser.add_argument("--pg-port", default="5432")
    parser.add_argument("--pg-db", default="transcria")
    parser.add_argument("--pg-user", default="transcria")
    parser.add_argument("--pg-migrate", action="store_true")
    parser.add_argument("--skip-doctor", action="store_true")
    parser.add_argument("--strict-doctor", action="store_true")
    pg = parser.add_mutually_exclusive_group()
    pg.add_argument("--postgres", action="store_true", help="force PostgreSQL")
    pg.add_argument("--sqlite-dev", "--allow-sqlite-dev", "--no-postgres", action="store_true", help="force SQLite dev local")
    args = parser.parse_args(argv)

    setup_postgres = None
    if args.postgres:
        setup_postgres = True
    elif args.sqlite_dev:
        setup_postgres = False
    if args.skip_doctor and args.strict_doctor:
        print("--skip-doctor et --strict-doctor sont incompatibles", file=sys.stderr)
        return 1

    try:
        plan = resolve_install_plan(
            args.profile,
            systemd=not args.no_systemd,
            setup_postgres=setup_postgres,
        )
    except ValueError as exc:
        parser.exit(1, f"{exc}\n")
    if args.format == "text":
        sys_stdout = render_install_plan_text(
            plan,
            PlanRenderContext(
                install_dir=args.install_dir,
                service_user=args.service_user,
                install_torch=not args.no_torch,
                postgres_host=args.pg_host,
                postgres_port=args.pg_port,
                postgres_db=args.pg_db,
                postgres_user=args.pg_user,
                postgres_migrate=args.pg_migrate,
                doctor_profile=args.profile,
                doctor_enabled=not args.skip_doctor,
                doctor_strict=args.strict_doctor,
            ),
        )
        print(sys_stdout, end="")
    elif args.format == "shell":
        print(render_install_plan_shell(plan), end="")
    else:
        print(json.dumps(plan.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

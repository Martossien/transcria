from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

DEFAULT_INSTALL_DIR = "/home/admin_ia/transcria"
DEFAULT_SERVICE_USER = "admin_ia"
DEFAULT_SERVICE_HOME = "/home/admin_ia"
DEFAULT_LOG_DIR = "/var/log"


@dataclass(frozen=True)
class SystemdRenderContext:
    install_dir: str
    service_user: str
    service_home: str
    inference_log_dir: str = DEFAULT_LOG_DIR
    legacy_log_file: str | None = None
    legacy_pid_file: str | None = None
    venv_dir: str | None = None


@dataclass(frozen=True)
class SystemdUnitPlan:
    kind: str
    source: str
    destination: str
    unit: str
    adapted_name: str
    missing_event: str
    missing_hint_event: str = ""
    path_kind: str = ""
    legacy_log_file: str = ""
    legacy_pid_file: str = ""
    inference_log_dir: str = DEFAULT_LOG_DIR


def build_unit_plan(
    *,
    profile: str,
    install_service: bool,
    install_inference: bool,
    install_systemd: bool,
    install_dir: str,
    service_user: str,
) -> list[SystemdUnitPlan]:
    """Construit le plan d'unités systemd à rendre et installer pour un profil."""
    if not install_systemd:
        return []

    plans: list[SystemdUnitPlan] = []
    if install_service:
        legacy_log_file = f"{DEFAULT_LOG_DIR}/transcrIA.log"
        legacy_pid_file = "/run/transcrIA.pid"
        path_kind = ""
        if service_user != "root":
            legacy_log_file = f"{install_dir}/logs/transcrIA.log"
            legacy_pid_file = f"{install_dir}/run/transcrIA.pid"
            path_kind = "legacy-service"
        plans.append(
            SystemdUnitPlan(
                kind="legacy",
                source=f"{install_dir}/transcria.service",
                destination="/etc/systemd/system/transcria.service",
                unit="transcria",
                adapted_name="transcria.service.adapted",
                missing_event="legacy-missing",
                path_kind=path_kind,
                legacy_log_file=legacy_log_file,
                legacy_pid_file=legacy_pid_file,
            )
        )

    if profile in {"web", "scheduler", "migrate"}:
        plans.append(
            SystemdUnitPlan(
                kind="split",
                source=f"{install_dir}/deploy/transcria-migrate.service",
                destination="/etc/systemd/system/transcria-migrate.service",
                unit="transcria-migrate",
                adapted_name="transcria-migrate.service.adapted",
                missing_event="missing-unit",
            )
        )
        if profile in {"web", "scheduler"}:
            unit = f"transcria-{profile}"
            plans.append(
                SystemdUnitPlan(
                    kind="split",
                    source=f"{install_dir}/deploy/{unit}.service",
                    destination=f"/etc/systemd/system/{unit}.service",
                    unit=unit,
                    adapted_name=f"{unit}.service.adapted",
                    missing_event="missing-unit",
                )
            )

    if install_inference:
        inference_log_dir = DEFAULT_LOG_DIR
        path_kind = ""
        if service_user != "root":
            inference_log_dir = f"{install_dir}/logs"
            path_kind = "inference-service"
        plans.append(
            SystemdUnitPlan(
                kind="inference",
                source=f"{install_dir}/deploy/transcria-inference.service",
                destination="/etc/systemd/system/transcria-inference.service",
                unit="transcria-inference",
                adapted_name="transcria-inference.service.adapted",
                missing_event="inference-missing",
                missing_hint_event="inference-missing-hint",
                path_kind=path_kind,
                inference_log_dir=inference_log_dir,
            )
        )

    return plans


def render_unit_plan_lines(plans: list[SystemdUnitPlan]) -> str:
    """Rend un plan systemd en lignes `|` stables pour orchestration shell filtrée."""
    rows = [
        "|".join(
            [
                plan.kind,
                plan.source,
                plan.destination,
                plan.unit,
                plan.adapted_name,
                plan.missing_event,
                plan.missing_hint_event,
                plan.path_kind,
                plan.legacy_log_file,
                plan.legacy_pid_file,
                plan.inference_log_dir,
            ]
        )
        for plan in plans
    ]
    return "\n".join(rows) + ("\n" if rows else "")


def render_split_unit(template: str, context: SystemdRenderContext) -> str:
    """Rend une unité split web/scheduler/migrate depuis le template versionné."""
    rendered = template.replace(DEFAULT_INSTALL_DIR, context.install_dir)
    rendered = rendered.replace(f"User={DEFAULT_SERVICE_USER}", f"User={context.service_user}")
    rendered = rendered.replace(f"HF_HOME={DEFAULT_SERVICE_HOME}/", f"HF_HOME={context.service_home}/")
    return rendered


def render_inference_unit(template: str, context: SystemdRenderContext) -> str:
    """Rend l'unité `transcria-inference.service` depuis le template versionné."""
    rendered = template.replace(
        f"ReadWritePaths={DEFAULT_LOG_DIR} {DEFAULT_INSTALL_DIR}",
        f"ReadWritePaths={context.inference_log_dir} {context.install_dir}",
    )
    rendered = rendered.replace(DEFAULT_INSTALL_DIR, context.install_dir)
    rendered = rendered.replace("User=root", f"User={context.service_user}")
    rendered = rendered.replace("Group=root", f"Group={context.service_user}")
    rendered = rendered.replace(
        f"{DEFAULT_LOG_DIR}/transcria-inference-access.log",
        f"{context.inference_log_dir}/transcria-inference-access.log",
    )
    rendered = rendered.replace(
        f"{DEFAULT_LOG_DIR}/transcria-inference-error.log",
        f"{context.inference_log_dir}/transcria-inference-error.log",
    )
    rendered = rendered.replace(
        f"{DEFAULT_LOG_DIR}/transcria-inference.log",
        f"{context.inference_log_dir}/transcria-inference.log",
    )
    return rendered


def render_legacy_unit(template: str, context: SystemdRenderContext) -> str:
    """Rend l'unité historique `transcria.service` depuis le template versionné."""
    log_file = context.legacy_log_file or f"{DEFAULT_LOG_DIR}/transcrIA.log"
    pid_file = context.legacy_pid_file or "/run/transcrIA.pid"
    venv_dir = context.venv_dir or f"{context.install_dir}/venv"

    rendered = template.replace(DEFAULT_INSTALL_DIR, context.install_dir)
    rendered = rendered.replace("User=root", f"User={context.service_user}")
    rendered = rendered.replace("PIDFile=/run/transcrIA.pid", f"PIDFile={pid_file}")
    rendered = rendered.replace("Environment=LOG_FILE=/var/log/transcrIA.log", f"Environment=LOG_FILE={log_file}")
    rendered = rendered.replace("Environment=PID_FILE=/run/transcrIA.pid", f"Environment=PID_FILE={pid_file}")
    rendered = rendered.replace(f"Environment=VENV={DEFAULT_INSTALL_DIR}/venv", f"Environment=VENV={venv_dir}")
    rendered = rendered.replace(f"HF_HOME={DEFAULT_SERVICE_HOME}/", f"HF_HOME={context.service_home}/")
    return rendered


def render_setup_log(*, event: str, unit: str = "", adapted: str = "", dst: str = "") -> str:
    """Rend les messages d'installation systemd utilisés par install.sh (FR/EN).

    Préfixe (``OK:``/``INFO:``/``WARN:``) lu par install.sh, non localisé ; les lignes de
    commande (``sudo cp``/``systemctl``) restent littérales, seul le texte suit la langue."""
    from transcria.install_messages import t

    if event == "skipped":
        return f"INFO:{t('sys_skipped', unit=unit)}\n"
    if event == "installed":
        return f"OK:{t('sys_installed', unit=unit)}\n"
    if event == "sudo-missing":
        return f"WARN:{t('sys_sudo_missing', adapted=adapted)}\n"
    if event == "manual-title":
        return f"WARN:{t('sys_manual_title')}\n"
    if event == "manual-copy":
        return f"WARN:  sudo cp {adapted} {dst}\n"
    if event == "manual-enable":
        return f"WARN:  sudo systemctl daemon-reload && sudo systemctl enable {unit}\n"
    if event == "missing-unit":
        return f"WARN:{t('sys_missing_unit', unit=unit)}\n"
    if event == "legacy-missing":
        return f"WARN:{t('sys_legacy_missing')}\n"
    if event == "split-legacy-enabled":
        return f"WARN:{t('sys_split_legacy_enabled')}\n"
    if event == "split-legacy-disable-command":
        return "WARN:  sudo systemctl disable --now transcria.service\n"
    if event == "inference-missing":
        return f"WARN:{t('sys_inference_missing')}\n"
    if event == "inference-missing-hint":
        return f"WARN:{t('sys_inference_missing_hint')}\n"
    raise ValueError(f"événement systemd inconnu : {event}")


def parse_bool(value: str | bool, *, name: str) -> bool:
    """Parse un booléen CLI stable."""
    if isinstance(value, bool):
        return value
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} booléen invalide : {value}")


RunFn = Callable[..., subprocess.CompletedProcess[str]]


def install_rendered_unit(
    *,
    rendered: Path,
    destination: Path,
    unit: str,
    adapted: Path,
    euid: int,
    have_sudo: bool,
    run: RunFn = subprocess.run,
) -> str:
    """Installe une unité systemd rendue, ou écrit le fichier adapté si sudo manque."""
    if euid == 0:
        shutil.copy2(rendered, destination)
        destination.chmod(0o644)
        run(["systemctl", "daemon-reload"], check=True)
        run(["systemctl", "enable", unit], check=True)
        return render_setup_log(event="installed", unit=unit)

    if have_sudo:
        run(["sudo", "cp", str(rendered), str(destination)], check=True)
        run(["sudo", "chmod", "644", str(destination)], check=True)
        run(["sudo", "systemctl", "daemon-reload"], check=True)
        run(["sudo", "systemctl", "enable", unit], check=True)
        return render_setup_log(event="installed", unit=unit)

    shutil.copy2(rendered, adapted)
    return "".join(
        [
            render_setup_log(event="sudo-missing", unit=unit, adapted=str(adapted)),
            render_setup_log(event="manual-title"),
            render_setup_log(event="manual-copy", unit=unit, adapted=str(adapted), dst=str(destination)),
            render_setup_log(event="manual-enable", unit=unit),
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rend une unité systemd TranscrIA depuis un template versionné.")
    parser.add_argument("--kind", choices=("legacy", "split", "inference"), default=None)
    parser.add_argument("--template", default=None, help="chemin du template systemd")
    parser.add_argument("--install-dir", default=None)
    parser.add_argument("--service-user", default=None)
    parser.add_argument("--service-home", default=None)
    parser.add_argument("--inference-log-dir", default=DEFAULT_LOG_DIR)
    parser.add_argument("--legacy-log-file", default=None)
    parser.add_argument("--legacy-pid-file", default=None)
    parser.add_argument("--venv-dir", default=None)
    parser.add_argument("--setup-log", action="store_true", help="rend un message systemd")
    parser.add_argument("--unit-plan", action="store_true", help="rend le plan d'unités systemd à installer")
    parser.add_argument("--install-unit", action="store_true", help="installe une unité systemd rendue ou écrit le fichier adapté")
    parser.add_argument("--profile", default="all-in-one")
    parser.add_argument("--install-service", default="false")
    parser.add_argument("--install-inference", default="false")
    parser.add_argument("--install-systemd", default="true")
    parser.add_argument("--event", default="")
    parser.add_argument("--unit", default="")
    parser.add_argument("--adapted", default="")
    parser.add_argument("--dst", default="")
    parser.add_argument("--rendered", default=None)
    parser.add_argument("--euid", default=None)
    parser.add_argument("--have-sudo", default="false")
    args = parser.parse_args(argv)

    if args.unit_plan:
        if not args.install_dir or not args.service_user:
            print("arguments requis avec --unit-plan: --install-dir, --service-user", file=sys.stderr)
            return 2
        plans = build_unit_plan(
            profile=args.profile,
            install_service=args.install_service.lower() == "true",
            install_inference=args.install_inference.lower() == "true",
            install_systemd=args.install_systemd.lower() == "true",
            install_dir=args.install_dir,
            service_user=args.service_user,
        )
        print(render_unit_plan_lines(plans), end="")
        return 0

    if args.setup_log:
        if not args.event:
            print("--event requis avec --setup-log", file=sys.stderr)
            return 2
        try:
            print(render_setup_log(event=args.event, unit=args.unit, adapted=args.adapted, dst=args.dst), end="")
            return 0
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    if args.install_unit:
        missing = [
            name
            for name, value in (
                ("--rendered", args.rendered),
                ("--dst", args.dst),
                ("--unit", args.unit),
                ("--adapted", args.adapted),
                ("--euid", args.euid),
            )
            if not value
        ]
        if missing:
            print("arguments requis avec --install-unit: " + ", ".join(missing), file=sys.stderr)
            return 2
        try:
            output = install_rendered_unit(
                rendered=Path(args.rendered),
                destination=Path(args.dst),
                unit=args.unit,
                adapted=Path(args.adapted),
                euid=int(args.euid),
                have_sudo=parse_bool(args.have_sudo, name="have_sudo"),
            )
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(output, end="")
        return 0

    missing = [
        name
        for name, value in (
            ("--kind", args.kind),
            ("--template", args.template),
            ("--install-dir", args.install_dir),
            ("--service-user", args.service_user),
            ("--service-home", args.service_home),
        )
        if not value
    ]
    if missing:
        print("arguments requis: " + ", ".join(missing), file=sys.stderr)
        return 2

    template_path = Path(args.template)
    template = template_path.read_text(encoding="utf-8")
    context = SystemdRenderContext(
        install_dir=args.install_dir,
        service_user=args.service_user,
        service_home=args.service_home,
        inference_log_dir=args.inference_log_dir,
        legacy_log_file=args.legacy_log_file,
        legacy_pid_file=args.legacy_pid_file,
        venv_dir=args.venv_dir,
    )
    if args.kind == "legacy":
        rendered = render_legacy_unit(template, context)
    elif args.kind == "split":
        rendered = render_split_unit(template, context)
    else:
        rendered = render_inference_unit(template, context)
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

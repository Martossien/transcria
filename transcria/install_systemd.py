from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

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
    rendered = rendered.replace(f"TRANSFORMERS_CACHE={DEFAULT_SERVICE_HOME}/", f"TRANSFORMERS_CACHE={context.service_home}/")
    return rendered


def render_setup_log(*, event: str, unit: str = "", adapted: str = "", dst: str = "") -> str:
    """Rend les messages d'installation systemd utilisés par install.sh."""
    if event == "skipped":
        return f"INFO:Service {unit} non installé (--no-service)\n"
    if event == "installed":
        return f"OK:Service {unit} installé et activé\n"
    if event == "sudo-missing":
        return f"WARN:sudo indisponible — fichier adapté : {adapted}\n"
    if event == "manual-title":
        return "WARN:Pour installer :\n"
    if event == "manual-copy":
        return f"WARN:  sudo cp {adapted} {dst}\n"
    if event == "manual-enable":
        return f"WARN:  sudo systemctl daemon-reload && sudo systemctl enable {unit}\n"
    if event == "missing-unit":
        return f"WARN:{unit}.service introuvable — service non installé\n"
    if event == "legacy-missing":
        return "WARN:transcria.service introuvable — service non installé\n"
    if event == "split-legacy-enabled":
        return "WARN:transcria.service est déjà activé. En déploiement split, désactivez-le avant de démarrer web/scheduler :\n"
    if event == "split-legacy-disable-command":
        return "WARN:  sudo systemctl disable --now transcria.service\n"
    if event == "inference-missing":
        return "WARN:transcria-inference.service introuvable — service non installé\n"
    if event == "inference-missing-hint":
        return "WARN:  Vérifiez que deploy/transcria-inference.service existe.\n"
    raise ValueError(f"événement systemd inconnu : {event}")


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
    parser.add_argument("--event", default="")
    parser.add_argument("--unit", default="")
    parser.add_argument("--adapted", default="")
    parser.add_argument("--dst", default="")
    args = parser.parse_args(argv)

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

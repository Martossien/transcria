"""Phase « opencode » de l'installateur (détection / installation / configuration).

Troisième tranche fondue depuis `install.sh` (SECTION 9). Orchestration :
détecter le binaire opencode → s'il manque, proposer (interactif) de le télécharger
→ configurer le provider local via `setup_opencode.py`. Réutilise en process les
primitives de `transcria.install_opencode` (détection, install réseau, PATH, chown
best-effort) et son texte de log audité.

Tourne sous le **python du venv** (importe `config.yaml_file` / PyYAML) ; le `cli`
l'importe en différé pour ne pas charger PyYAML dans la phase `python-env` pré-venv.

Effets privilégiés/réseau (téléchargement, `chown` vers l'utilisateur de service) et
sous-processus (`setup_opencode.py`) passent par des dépendances injectables, ce qui
rend l'orchestration testable sans réseau ni privilèges.
"""
from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from transcria.config.yaml_file import get_yaml_value, load_yaml_file, set_yaml_file_value
from transcria.install_opencode import (
    OpencodeDetection,
    _best_effort_chown_tree,
    detect_opencode,
    ensure_shell_path,
    install_opencode_binary,
    render_install_prompt,
    render_setup_log,
)

Runner = Callable[..., Any]
ConfirmFn = Callable[[str], bool]
ChownFn = Callable[[Path, str], None]
DetectFn = Callable[[], OpencodeDetection]
_OPENCODE_BIN_KEY = "workflow.arbitration_llm.opencode_bin"
_DOWNLOAD_URL = "https://github.com/anomalyco/opencode/releases/latest/download/opencode-linux-x64"


class _ConsoleLike(Protocol):
    def info(self, message: str) -> None: ...
    def ok(self, message: str) -> None: ...
    def warn(self, message: str) -> None: ...
    def error(self, message: str) -> None: ...


@dataclass(frozen=True)
class OpencodePlan:
    install_dir: Path
    config_path: Path
    opencode_home: Path
    user_home: Path
    service_user: str = ""
    profile: str = ""
    needs_llm: bool = True
    interactive: bool = True
    current_path: str = ""
    rc_files: tuple[Path, ...] = ()
    venv_python: Path | None = None
    download_url: str = _DOWNLOAD_URL


@dataclass
class OpencodeResult:
    actions: list[str] = field(default_factory=list)

    def record(self, action: str) -> None:
        self.actions.append(action)


def _emit(console: _ConsoleLike, event: str, *, value: str = "", profile: str = "") -> None:
    rendered = render_setup_log(event=event, value=value, profile=profile).rstrip("\n")
    prefix, _, message = rendered.partition(":")
    {"OK": console.ok, "INFO": console.info, "WARN": console.warn, "ERROR": console.error}.get(prefix, console.info)(message)


def _configured_bin(config_path: Path) -> str | None:
    value = get_yaml_value(load_yaml_file(config_path), _OPENCODE_BIN_KEY)
    return str(value) if value else None


def apply_opencode(
    plan: OpencodePlan,
    *,
    console: _ConsoleLike,
    runner: Runner = subprocess.run,
    confirm: ConfirmFn | None = None,
    chown: ChownFn = _best_effort_chown_tree,
    detect: DetectFn | None = None,
) -> OpencodeResult:
    """Détecte/installe/configure opencode (cœur de SECTION 9)."""
    result = OpencodeResult()
    if not plan.needs_llm:
        _emit(console, "profile-skipped", profile=plan.profile)
        result.record("profile-skipped")
        return result

    confirm = confirm if confirm is not None else (lambda _prompt: False)
    if detect is None:
        def detect() -> OpencodeDetection:
            return detect_opencode(
                opencode_home=plan.opencode_home,
                user_home=plan.user_home,
                configured_bin=_configured_bin(plan.config_path),
            )

    detection = detect()
    binary: Path | None = detection.binary

    if binary is not None:
        _emit(console, "found", value=f"{binary} ({detection.version})")
        set_yaml_file_value(plan.config_path, _OPENCODE_BIN_KEY, str(binary))
        result.record("found")
    else:
        _emit(console, "missing")
        result.record("missing")
        if confirm(render_install_prompt(opencode_home=plan.opencode_home)):
            destination = plan.opencode_home / ".opencode" / "bin" / "opencode"
            _emit(console, "download-start")
            ok = install_opencode_binary(
                destination=destination,
                url=plan.download_url,
                service_user=plan.service_user,
                owner_root=plan.opencode_home / ".opencode",
                run=runner,
            )
            if ok:
                _emit(console, "installed", value=str(destination))
                binary = destination
                set_yaml_file_value(plan.config_path, _OPENCODE_BIN_KEY, str(destination))
                result.record("installed")
                updated = ensure_shell_path(destination.parent, list(plan.rc_files), current_path=plan.current_path)
                if updated is not None:
                    _emit(console, "path-updated", value=str(updated))
                    _emit(console, "shell-reload", value=str(destination.parent))
            else:
                for event in ("download-failed", "manual-title", "manual-mkdir", "manual-curl", "manual-chmod"):
                    _emit(console, event)
                result.record("download-failed")
        else:
            _emit(console, "ignored")
            _emit(console, "install-later")
            result.record("ignored")

    if binary is not None:
        _configure_provider(plan, console, runner, chown, result)

    return result


def _configure_provider(
    plan: OpencodePlan, console: _ConsoleLike, runner: Runner, chown: ChownFn, result: OpencodeResult
) -> None:
    _emit(console, "configure-start")
    venv_python = str(plan.venv_python or sys.executable)
    config_json = plan.opencode_home / ".config" / "opencode" / "opencode.json"
    script = plan.install_dir / "scripts" / "setup_opencode.py"
    proc = runner([venv_python, str(script), "--config-path", str(config_json)], check=False)
    if getattr(proc, "returncode", 1) == 0:
        chown(plan.opencode_home / ".config" / "opencode", plan.service_user)
        _emit(console, "provider-ok")
        result.record("provider-ok")
    else:
        _emit(console, "provider-incomplete", value=f"{venv_python} scripts/setup_opencode.py")
        result.record("provider-incomplete")

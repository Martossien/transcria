"""Phase « services systemd » de l'installateur.

Cinquième tranche fondue depuis `install.sh` (SECTION 11). Couvre l'orchestration
d'installation des unités systemd du profil : construction du plan d'unités,
avertissement legacy en déploiement split, et pour chaque unité — vérification du
template, préparation/chown des répertoires de logs, rendu depuis le template versionné
et installation privilégiée (copie + `daemon-reload` + `enable`, ou écriture d'un fichier
`.adapted` quand `sudo` manque).

La logique métier (plan, rendu, installation) vit déjà dans `transcria.install_systemd`
(fonctions pures + `install_rendered_unit` à runner injectable) : cette phase l'appelle
**en process** au lieu de la piloter via des lignes `|` reparsées par le shell. Les
opérations système (systemctl, chown récursif, existence d'utilisateur, création de
répertoires) sont toutes **injectables** pour des tests sans privilèges ni systemd réel.

Tourne sous le python du venv ; le filet E2E l'exerce avec `--no-service` (plan vide,
aucune unité installée), donc l'installation privilégiée réelle est couverte par les
tests unitaires (runner injecté).
"""
from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from transcria.install_messages import t
from transcria.install_paths import directory_specs_for_kind, ensure_directories
from transcria.install_systemd import (
    SystemdRenderContext,
    SystemdUnitPlan,
    build_unit_plan,
    install_rendered_unit,
    render_inference_unit,
    render_legacy_unit,
    render_setup_log,
    render_split_unit,
)

Runner = Callable[..., subprocess.CompletedProcess]
SystemctlEnabled = Callable[[str], bool]
UserExists = Callable[[str], bool]
Chown = Callable[[Path, str], None]
EnsurePaths = Callable[[str, Path], None]

_TAGS = ("OK", "INFO", "WARN", "ERROR")
_RENDERERS = {"split": render_split_unit, "inference": render_inference_unit, "legacy": render_legacy_unit}
_SPLIT_PROFILES = ("web", "scheduler", "migrate")


class _ConsoleLike(Protocol):
    def info(self, message: str) -> None: ...
    def ok(self, message: str) -> None: ...
    def warn(self, message: str) -> None: ...
    def error(self, message: str) -> None: ...
    def section(self, title: str) -> None: ...


@dataclass(frozen=True)
class SystemdPlan:
    profile: str
    install_dir: Path
    service_user: str
    service_home: str
    venv_dir: Path
    install_service: bool = True
    install_inference: bool = False
    install_systemd: bool = True
    euid: int = 0
    have_sudo: bool = False
    have_systemctl: bool = False


@dataclass
class SystemdResult:
    actions: list[str] = field(default_factory=list)

    def record(self, action: str) -> None:
        self.actions.append(action)


def _emit_text(console: _ConsoleLike, text: str) -> None:
    """Route chaque ligne rendue (`OK:`/`INFO:`/`WARN:`/`ERROR:`) vers la console."""
    methods = {"OK": console.ok, "INFO": console.info, "WARN": console.warn, "ERROR": console.error}
    for line in text.splitlines():
        if not line:
            continue
        tag, sep, rest = line.partition(":")
        if sep and tag in _TAGS:
            methods[tag](rest)
        else:
            console.info(line)


def _default_systemctl_enabled(unit: str) -> bool:
    return subprocess.run(["systemctl", "is-enabled", "--quiet", unit], check=False).returncode == 0


def _default_user_exists(user: str) -> bool:
    import pwd

    try:
        pwd.getpwnam(user)
    except KeyError:
        return False
    return True


def _best_effort_chown_tree(path: Path, user: str) -> None:
    """chown -R best-effort (fidèle au `chown -R … || true` du shell, en root)."""
    import shutil

    if not path.exists():
        return
    try:
        shutil.chown(path, user=user)
        for child in path.rglob("*"):
            shutil.chown(child, user=user)
    except (LookupError, PermissionError, OSError):
        pass


def _default_ensure_paths(kind: str, install_dir: Path) -> None:
    ensure_directories(directory_specs_for_kind(kind, install_dir))


def _prepare_paths(up: SystemdUnitPlan, plan: SystemdPlan, *, chown: Chown, user_exists: UserExists, ensure_paths: EnsurePaths) -> None:
    if not up.path_kind:
        return
    ensure_paths(up.path_kind, plan.install_dir)
    if not user_exists(plan.service_user):
        return
    if up.path_kind == "legacy-service":
        chown(Path(up.legacy_log_file).parent, plan.service_user)
        chown(Path(up.legacy_pid_file).parent, plan.service_user)
    elif up.path_kind == "inference-service":
        chown(Path(up.inference_log_dir), plan.service_user)


def _install_one(up: SystemdUnitPlan, plan: SystemdPlan, console: _ConsoleLike, run: Runner) -> None:
    context = SystemdRenderContext(
        install_dir=str(plan.install_dir),
        service_user=plan.service_user,
        service_home=plan.service_home,
        inference_log_dir=up.inference_log_dir,
        legacy_log_file=up.legacy_log_file or None,
        legacy_pid_file=up.legacy_pid_file or None,
        venv_dir=str(plan.venv_dir),
    )
    rendered_text = _RENDERERS[up.kind](Path(up.source).read_text(encoding="utf-8"), context)

    with tempfile.NamedTemporaryFile("w", suffix=".service", delete=False, encoding="utf-8") as tmp:
        tmp.write(rendered_text)
        tmp_path = Path(tmp.name)
    try:
        output = install_rendered_unit(
            rendered=tmp_path,
            destination=Path(up.destination),
            unit=up.unit,
            adapted=plan.install_dir / up.adapted_name,
            euid=plan.euid,
            have_sudo=plan.have_sudo,
            run=run,
        )
    finally:
        tmp_path.unlink(missing_ok=True)
    _emit_text(console, output)


def apply_systemd(
    plan: SystemdPlan,
    *,
    console: _ConsoleLike,
    run: Runner = subprocess.run,
    systemctl_enabled: SystemctlEnabled = _default_systemctl_enabled,
    user_exists: UserExists = _default_user_exists,
    chown: Chown = _best_effort_chown_tree,
    ensure_paths: EnsurePaths = _default_ensure_paths,
) -> SystemdResult:
    """Installe les unités systemd du profil (cf. docstring du module)."""
    result = SystemdResult()
    unit_plans = build_unit_plan(
        profile=plan.profile,
        install_service=plan.install_service,
        install_inference=plan.install_inference,
        install_systemd=plan.install_systemd,
        install_dir=str(plan.install_dir),
        service_user=plan.service_user,
    )
    if not unit_plans:
        return result  # plan vide (ex. --no-service) : aucune section, fidèle au shell

    console.section(t("phase_systemd_section"))

    # Avertissement : transcria.service legacy encore activé en déploiement split.
    if plan.profile in _SPLIT_PROFILES and plan.have_systemctl and systemctl_enabled("transcria"):
        _emit_text(console, render_setup_log(event="split-legacy-enabled"))
        _emit_text(console, render_setup_log(event="split-legacy-disable-command"))
        result.record("split-legacy-warned")

    for up in unit_plans:
        if not Path(up.source).is_file():
            _emit_text(console, render_setup_log(event=up.missing_event, unit=up.unit))
            if up.missing_hint_event:
                _emit_text(console, render_setup_log(event=up.missing_hint_event))
            result.record(f"missing:{up.unit}")
            continue

        _prepare_paths(up, plan, chown=chown, user_exists=user_exists, ensure_paths=ensure_paths)
        _install_one(up, plan, console, run)
        result.record(f"installed:{up.unit}")

    return result

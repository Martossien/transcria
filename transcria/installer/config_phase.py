"""Phase « configuration » de l'installateur (config.yaml + .env + secrets + rôle).

Deuxième tranche fondue depuis `install.sh` (SECTION 6, cœur déterministe). Couvre :
génération de `config.yaml` via `bootstrap_config.py` (avec sauvegarde si `--force`
sur un fichier existant), initialisation de `.env`, garantie de `TRANSCRIA_SECRET`,
écriture du rôle runtime (`runtime.role` + `TRANSCRIA_ROLE`) et, pour un nœud de
ressources, de `TRANSCRIA_INFERENCE_API_KEY`.

Le bloc proxy d'entreprise (interactif) reste volontairement dans `install.sh` : il
prompte l'utilisateur et applique un `chmod`/`chown` privilégié, hors du périmètre
déterministe et testable de cette phase.

Contrairement à la phase environnement Python (pré-venv, interpréteur système), cette
phase tourne sous le **python du venv** : elle importe `config.yaml_file` (PyYAML) et
lance `bootstrap_config.py` (dépendances du projet). Les messages reprennent au mot
près ceux de `transcria.install_summary` (texte audité et déjà testé).
"""
from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol

from transcria.config.env_file import (
    ensure_env_secret,
    has_any_env_key,
    init_env_file_from_template,
    update_env_file,
)
from transcria.config.yaml_file import backup_yaml_file, set_yaml_file_value
from transcria.install_summary import render_setup_log

Runner = Callable[..., Any]
_INFERENCE_KEY_COMMENT = "Clé API du service inference_service (/infer/* et /engines/*)."
_PROXY_COMMENT = "Proxy d'entreprise — requis par le service systemd pour télécharger les modèles (docs/INSTALL.md § Réseau d'entreprise)."
ProxyConfirm = Callable[[str], bool]
ProxyChown = Callable[[Path, str], None]


class _ConsoleLike(Protocol):
    def info(self, message: str) -> None: ...
    def ok(self, message: str) -> None: ...
    def warn(self, message: str) -> None: ...
    def error(self, message: str) -> None: ...


@dataclass(frozen=True)
class ConfigPlan:
    install_dir: Path
    config_path: Path
    env_file: Path
    example_config: Path
    env_template: Path
    profile: str
    runtime_role: str = ""
    profile_explicit: bool = False
    install_inference: bool = False
    force_config: bool = False
    venv_python: Path | None = None
    backup_suffix: str | None = None  # injectable pour des tests déterministes


@dataclass(frozen=True)
class ProxyPlan:
    env_file: Path
    proxy_https: str
    proxy_http: str
    proxy_no: str
    service_user: str = ""
    is_root: bool = False
    interactive: bool = True


@dataclass
class ConfigResult:
    actions: list[str] = field(default_factory=list)

    def record(self, action: str) -> None:
        self.actions.append(action)


def _emit(console: _ConsoleLike, event: str, *, profile: str = "", runtime_role: str = "", value: str = "") -> None:
    """Affiche un message via le renderer audité, traduit son préfixe pour la console."""
    rendered = render_setup_log(event=event, profile=profile, runtime_role=runtime_role, value=value).rstrip("\n")
    prefix, _, message = rendered.partition(":")
    {"OK": console.ok, "INFO": console.info, "WARN": console.warn}.get(prefix, console.info)(message)


def _generate_config(plan: ConfigPlan, console: _ConsoleLike, runner: Runner, result: ConfigResult) -> None:
    if plan.config_path.exists() and not plan.force_config:
        _emit(console, "config-kept")
        _emit(console, "force-hint")
        result.record("config-kept")
        return

    if plan.config_path.exists() and plan.force_config:
        suffix = plan.backup_suffix or datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = backup_yaml_file(plan.config_path, suffix)
        _emit(console, "config-backup", value=str(backup))
        result.record("config-backup")

    _emit(console, "config-generate-start")
    venv_python = str(plan.venv_python or sys.executable)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(plan.install_dir), os.environ.get("PYTHONPATH", "")]))}
    cmd: Sequence[str] = [
        venv_python,
        str(plan.install_dir / "scripts" / "bootstrap_config.py"),
        "--example", str(plan.example_config),
        "--output", str(plan.config_path),
        "--profile", plan.profile,
        "--force",
    ]
    runner(list(cmd), check=True, env=env)
    _emit(console, "config-generated")
    result.record("config-generated")


def _write_runtime_role(plan: ConfigPlan, console: _ConsoleLike, result: ConfigResult) -> None:
    # Rôle explicite écrit sauf pour all-in-one implicite (défaut historique).
    if plan.runtime_role and (plan.profile != "all-in-one" or plan.profile_explicit):
        set_yaml_file_value(plan.config_path, "runtime.role", plan.runtime_role)
        update_env_file(plan.env_file, "TRANSCRIA_ROLE", plan.runtime_role, backup=False)
        _emit(console, "profile-runtime", profile=plan.profile, runtime_role=plan.runtime_role)
        result.record("profile-runtime")
        return
    event = {
        "all-in-one": "profile-all-default",
        "resource-node": "profile-resource-node",
        "migrate": "profile-migrate",
    }.get(plan.profile, "profile-generic")
    _emit(console, event, profile=plan.profile)
    result.record(event)


def apply_config(plan: ConfigPlan, *, console: _ConsoleLike, runner: Runner = subprocess.run) -> ConfigResult:
    """Génère config.yaml/.env et les variables dérivées du profil (cœur déterministe)."""
    result = ConfigResult()

    _generate_config(plan, console, runner, result)

    # .env depuis le template (no-op si déjà présent).
    init_env_file_from_template(plan.env_file, plan.env_template)

    # Clé secrète Flask (8 octets hex mini ; remplace le placeholder du template).
    secret_status = ensure_env_secret(
        plan.env_file, "TRANSCRIA_SECRET",
        min_length=8, placeholder="change-me-to-a-random-secret", generator="hex",
    )
    _emit(console, "secret-created" if secret_status == "created" else "secret-present")
    result.record(f"secret-{secret_status}")

    _write_runtime_role(plan, console, result)

    if plan.install_inference:
        key_status = ensure_env_secret(
            plan.env_file, "TRANSCRIA_INFERENCE_API_KEY",
            min_length=16, placeholder=None, generator="urlsafe", comment=_INFERENCE_KEY_COMMENT,
        )
        _emit(console, "inference-key-present" if key_status == "present" else "inference-key-created")
        result.record(f"inference-key-{key_status}")

    return result


def _default_proxy_confirm(proxy_https: str) -> bool:
    """Prompt `ask_yn`-fidèle : seul o/O/y/Y persiste le proxy."""
    try:
        answer = input(f"  Proxy détecté ({proxy_https}) : le persister dans .env pour le service ? [o/N] : ")
    except EOFError:
        return False
    return answer.strip() in ("o", "O", "y", "Y")


def _best_effort_chown(path: Path, service_user: str) -> None:
    import shutil

    try:
        shutil.chown(path, user=service_user)
    except (LookupError, PermissionError, OSError):
        pass


def apply_proxy(
    plan: ProxyPlan,
    *,
    console: _ConsoleLike,
    confirm: ProxyConfirm = _default_proxy_confirm,
    chown: ProxyChown = _best_effort_chown,
) -> ConfigResult:
    """Persiste le proxy d'entreprise dans `.env` pour le service systemd.

    Le service n'hérite pas de l'environnement du shell : sans cette persistance, un
    proxy connu du seul shell fait *pendre* les téléchargements de modèles côté service.
    Le bloc reste *déclenché* par le shell (qui lit son propre environnement) ; ici on
    décide, on confirme et on écrit. En non-interactif on persiste (fidèle à `ask_yn`,
    non appelé dans ce mode).
    """
    result = ConfigResult()
    if has_any_env_key(plan.env_file, ["http_proxy", "https_proxy"]):
        _emit(console, "proxy-present")
        result.record("proxy-present")
        return result

    persist = True if not plan.interactive else confirm(plan.proxy_https)
    if not persist:
        result.record("proxy-skipped")
        return result

    update_env_file(plan.env_file, "http_proxy", plan.proxy_http, backup=False, comment=_PROXY_COMMENT)
    update_env_file(plan.env_file, "https_proxy", plan.proxy_https, backup=False)
    update_env_file(plan.env_file, "no_proxy", plan.proxy_no, backup=False)
    if plan.is_root and plan.service_user:
        chown(plan.env_file, plan.service_user)
    _emit(console, "proxy-persisted")
    result.record("proxy-persisted")
    return result

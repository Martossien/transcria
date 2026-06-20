"""Tests unitaires de la phase « services systemd » de l'installateur.

systemctl, chown récursif, existence d'utilisateur, création de répertoires et le
runner d'installation sont injectés : on vérifie l'orchestration (plan vide, template
absent, avertissement legacy split, préparation/chown des répertoires, installation via
sudo, repli `.adapted` sans sudo) sans systemd ni privilèges réels — et jamais d'écriture
vers `/etc` (le chemin root de `install_rendered_unit` est couvert par test_install_systemd).
"""
from __future__ import annotations

import io
from pathlib import Path

from transcria.installer.console import Console
from transcria.installer.systemd_phase import SystemdPlan, apply_systemd


def _console_pair():
    out = io.StringIO()
    return Console(out, color=False), out


class _Run:
    def __init__(self, returncode: int = 0):
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def __call__(self, cmd, check=False):
        self.calls.append(list(cmd))

        rc = self.returncode

        class _CP:
            returncode = rc
            stdout = ""
            stderr = ""

        return _CP()


def _plan(tmp_path: Path, **kw) -> SystemdPlan:
    defaults = dict(
        profile="all-in-one",
        install_dir=tmp_path,
        service_user="root",
        service_home="/root",
        venv_dir=tmp_path / "venv",
        install_service=True,
        install_inference=False,
        install_systemd=True,
        euid=1000,
        have_sudo=False,
        have_systemctl=False,
    )
    defaults.update(kw)
    return SystemdPlan(**defaults)


def test_no_systemd_returns_empty_and_no_section(tmp_path):
    console, out = _console_pair()
    result = apply_systemd(_plan(tmp_path, install_systemd=False), console=console, run=_Run())
    assert result.actions == []
    assert "Services systemd" not in out.getvalue()


def test_missing_template_emits_missing_event(tmp_path):
    console, out = _console_pair()
    run = _Run()
    # all-in-one + install_service → unité legacy, mais install_dir/transcria.service absent.
    result = apply_systemd(_plan(tmp_path), console=console, run=run)
    assert any(a.startswith("missing:") for a in result.actions)
    assert "transcria.service introuvable" in out.getvalue()
    assert run.calls == []  # rien installé


def test_installs_unit_via_sudo(tmp_path):
    (tmp_path / "transcria.service").write_text("[Unit]\nDescription=t\nUser=admin_ia\n", encoding="utf-8")
    console, out = _console_pair()
    run = _Run()
    result = apply_systemd(_plan(tmp_path, service_user="root", euid=1000, have_sudo=True), console=console, run=run)

    assert any(c[:2] == ["sudo", "cp"] for c in run.calls)
    assert any(c[:3] == ["sudo", "systemctl", "daemon-reload"] for c in run.calls)
    assert any(c[:3] == ["sudo", "systemctl", "enable"] for c in run.calls)
    assert "installed:transcria" in result.actions
    assert "Services systemd" in out.getvalue()


def test_no_sudo_writes_adapted_and_manual_hint(tmp_path):
    (tmp_path / "transcria.service").write_text("[Unit]\n", encoding="utf-8")
    console, out = _console_pair()
    run = _Run()
    apply_systemd(_plan(tmp_path, service_user="root", euid=1000, have_sudo=False), console=console, run=run)

    assert (tmp_path / "transcria.service.adapted").is_file()
    assert run.calls == []  # aucune commande privilégiée tentée
    assert "sudo cp" in out.getvalue()  # consigne manuelle


def test_split_profile_warns_legacy_enabled(tmp_path):
    console, out = _console_pair()
    result = apply_systemd(
        _plan(tmp_path, profile="web", install_service=False, have_systemctl=True),
        console=console, run=_Run(), systemctl_enabled=lambda unit: True,
    )
    assert "split-legacy-warned" in result.actions
    assert "déjà activé" in out.getvalue()


def test_split_profile_no_warn_when_systemctl_absent(tmp_path):
    console, _ = _console_pair()
    calls: list[str] = []
    result = apply_systemd(
        _plan(tmp_path, profile="web", install_service=False, have_systemctl=False),
        console=console, run=_Run(), systemctl_enabled=lambda unit: calls.append(unit) or True,
    )
    assert "split-legacy-warned" not in result.actions
    assert calls == []  # sonde systemctl jamais appelée sans HAVE_SYSTEMCTL


def test_path_kind_triggers_ensure_and_chown(tmp_path):
    (tmp_path / "transcria.service").write_text("[Unit]\n", encoding="utf-8")
    console, _ = _console_pair()
    ensure_calls: list[tuple[str, str]] = []
    chown_calls: list[tuple[str, str]] = []

    # service_user != root → l'unité legacy porte path_kind=legacy-service.
    apply_systemd(
        _plan(tmp_path, service_user="svc", euid=1000, have_sudo=True), console=console, run=_Run(),
        ensure_paths=lambda kind, d: ensure_calls.append((kind, str(d))),
        chown=lambda p, u: chown_calls.append((str(p), u)),
        user_exists=lambda u: True,
    )
    assert ("legacy-service", str(tmp_path)) in ensure_calls
    assert chown_calls and all(u == "svc" for _, u in chown_calls)


def test_chown_skipped_when_user_absent(tmp_path):
    (tmp_path / "transcria.service").write_text("[Unit]\n", encoding="utf-8")
    console, _ = _console_pair()
    chown_calls: list = []
    apply_systemd(
        _plan(tmp_path, service_user="svc", euid=1000, have_sudo=True), console=console, run=_Run(),
        ensure_paths=lambda kind, d: None,
        chown=lambda p, u: chown_calls.append((p, u)),
        user_exists=lambda u: False,
    )
    assert chown_calls == []  # pas de chown si l'utilisateur de service n'existe pas

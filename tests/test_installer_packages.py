"""Socle des offres d'installation : famille de distro, commandes, verdict par le binaire.

Partagé par l'offre `ffmpeg` (issue #9) et l'offre « serveur PostgreSQL » (issue #14).
Les trois pièges verrouillés ici viennent tous d'un rejeu RÉEL en conteneur, aucun d'une
relecture : famille déduite d'un binaire, binaire absent qui lève, cache apt vide.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from transcria.installer.console import Console
from transcria.installer.packages import (
    PACKAGES,
    Step,
    can_offer,
    describe_command,
    detect_package_manager,
    ensure_package,
    install_steps,
    privilege_prefix,
    render_setup_log,
    run_command,
    run_steps,
)


def _which(*present: str):
    return lambda name: f"/usr/bin/{name}" if name in present else None


def _os_release(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "os-release"
    path.write_text(content, encoding="utf-8")
    return path


# ── Famille de distribution ────────────────────────────────────────────────────

def test_famille_lue_dans_os_release_pas_deduite_du_binaire(tmp_path):
    """Vécu sur la machine de dev : une Fedora 42 qui porte `/usr/bin/apt-get`.

    Prendre « le gestionnaire que je trouve » y lançait un `apt-get install` sur RPM."""
    fedora = _os_release(tmp_path, 'ID=fedora\nVERSION_ID="42"\n')

    assert detect_package_manager(which=_which("apt-get", "dnf"), os_release=fedora) == "dnf"


def test_famille_debian_derivee_par_id_like(tmp_path):
    mint = _os_release(tmp_path, 'ID=linuxmint\nID_LIKE="ubuntu debian"\n')

    assert detect_package_manager(which=_which("apt-get"), os_release=mint) == "apt"


def test_famille_inconnue_prefere_rpm(tmp_path):
    inconnue = _os_release(tmp_path, "ID=exotique\n")

    assert detect_package_manager(which=_which("apt-get", "dnf"), os_release=inconnue) == "dnf"
    assert detect_package_manager(which=_which("apt-get"), os_release=inconnue) == "apt"


def test_os_release_illisible_ne_leve_pas(tmp_path):
    assert detect_package_manager(which=_which(), os_release=tmp_path / "absent") == ""


# ── Privilèges ─────────────────────────────────────────────────────────────────

def test_prefixe_privilegie_selon_le_compte():
    assert privilege_prefix(which=_which("sudo"), is_root=True) == ()
    assert privilege_prefix(which=_which("sudo"), is_root=False) == ("sudo",)
    assert privilege_prefix(which=_which(), is_root=False) == ()


# ── Commandes ──────────────────────────────────────────────────────────────────

def test_ffmpeg_est_ffmpeg_free_sur_rpm():
    """Fedora n'a pas `ffmpeg` en dépôt de BASE mais `ffmpeg-free` (décodeurs aac/mp3/
    opus/flac vérifiés) : on ne devine aucun dépôt tiers type RPM Fusion."""
    assert PACKAGES["ffmpeg"]["dnf"] == ("ffmpeg-free",)
    assert describe_command("ffmpeg", manager="dnf") == "dnf install ffmpeg-free"
    assert describe_command("ffmpeg", manager="apt") == "apt-get install ffmpeg"


def test_commandes_apt_ferment_stdin_et_retentent_apres_update():
    steps = install_steps("ffmpeg", manager="apt", prefix=("sudo",))

    assert steps[0].argv == ("sudo", "env", "DEBIAN_FRONTEND=noninteractive",
                             "apt-get", "install", "-y", "ffmpeg")
    assert steps[0].recover == ("sudo", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "update")


def test_paquet_inconnu_pour_cette_famille_ne_produit_aucune_commande():
    assert install_steps("ffmpeg", manager="") == []
    assert describe_command("ffmpeg", manager="") == ""


# ── Exécution ──────────────────────────────────────────────────────────────────

def test_run_steps_retente_apres_recover_puis_bascule_sur_le_fallback():
    joues: list[tuple[str, ...]] = []

    def _run(cmd):
        joues.append(tuple(cmd))
        return 0 if tuple(cmd) == ("plan-b",) else 1

    echecs = run_steps([Step(("plan-a",), recover=("update",), fallback=("plan-b",))], run=_run)

    assert joues == [("plan-a",), ("update",), ("plan-a",), ("plan-b",)]
    assert echecs == []  # le fallback a réussi : l'étape n'est pas en échec


def test_run_steps_tolere_les_etapes_deja_faites():
    assert run_steps([Step(("initdb",), tolerate_failure=True)], run=lambda cmd: 1) == []


def test_run_command_ne_leve_jamais_sur_binaire_absent():
    """Trouvé au rejeu réel (fedora:42 sans `service`) : l'exception sortait en traceback."""
    assert run_command(["binaire-qui-n-existe-pas-du-tout"]) == 127


# ── Verdict ────────────────────────────────────────────────────────────────────

def test_le_verdict_vient_du_binaire_pas_du_gestionnaire(tmp_path):
    """dnf peut sortir en 0 sans que le binaire soit là (dépôt partiel) : on re-sonde."""
    stream = io.StringIO()

    ok = ensure_package(
        "ffmpeg", binary="ffmpeg", console=Console(stream, color=False),
        which=_which("dnf"), run=lambda cmd: 0, is_root=True,
        os_release=_os_release(tmp_path, "ID=fedora\n"),
    )

    assert ok is False
    assert "toujours absent" in stream.getvalue()


def test_installation_reussie_est_annoncee(tmp_path):
    stream = io.StringIO()

    ok = ensure_package(
        "ffmpeg", binary="ffmpeg", console=Console(stream, color=False),
        which=_which("dnf", "ffmpeg"), run=lambda cmd: 0, is_root=True,
        os_release=_os_release(tmp_path, "ID=fedora\n"),
    )

    assert ok is True
    assert "dnf install ffmpeg-free" in stream.getvalue()
    assert "ffmpeg installé" in stream.getvalue()


def test_aucune_offre_sans_famille_connue_ni_droits():
    assert can_offer("ffmpeg", manager="", prefix=(), is_root=True) is False
    assert can_offer("ffmpeg", manager="apt", prefix=(), is_root=False) is False
    assert can_offer("ffmpeg", manager="apt", prefix=("sudo",), is_root=False) is True
    assert can_offer("inconnu", manager="apt", prefix=(), is_root=True) is False


def test_render_setup_log_refuse_un_evenement_inconnu():
    with pytest.raises(ValueError, match="événement d'installation de paquet inconnu"):
        render_setup_log(event="inventé")

"""Offre « serveur PostgreSQL » — sonde, décision, commandes, re-sonde (issue #14).

Le repli SQLite était annoncé mais subi : l'utilisateur l'apprenait quand il ne pouvait
plus rien y faire. Ces tests verrouillent la forme retenue (celle de l'offre ffmpeg) :
on propose l'action JUSTE (installer vs démarrer), on joue les commandes de la bonne
famille de distribution, et c'est la sonde REJOUÉE qui décide du verdict.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from transcria.installer.console import Console
from transcria.installer.postgres_server import (
    ACTION_IMPOSSIBLE,
    ACTION_INSTALL,
    ACTION_NONE,
    ACTION_START,
    ServerState,
    decide_action,
    ensure_server,
    plan_steps,
    probe_server,
    render_probe_shell,
    render_setup_log,
)


def _which(*present: str):
    return lambda name: f"/usr/bin/{name}" if name in present else None


def _os_release(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "os-release"
    path.write_text(content, encoding="utf-8")
    return path


# ── Sonde ──────────────────────────────────────────────────────────────────────

def test_probe_serveur_muet_est_detecte(tmp_path):
    debian = _os_release(tmp_path, "ID=debian\n")

    state = probe_server(
        which=_which("psql", "pg_isready", "apt-get", "systemctl", "sudo"),
        run=lambda cmd: 2,  # pg_isready : aucun serveur
        is_root=False,
        os_release=debian,
    )

    assert state.psql is True
    assert state.reachable is False
    assert state.can_admin is True
    assert state.sudo_prefix == ("sudo",)


def test_probe_sans_pg_isready_ne_refuse_pas_postgres(tmp_path):
    """Client présent sans `pg_isready` (install exotique) : on ne fabrique pas un refus."""
    state = probe_server(
        which=_which("psql", "apt-get"),
        run=lambda cmd: pytest.fail("aucune sonde ne doit être lancée"),
        is_root=True,
        os_release=_os_release(tmp_path, "ID=debian\n"),
    )

    assert state.reachable is True
    assert state.sudo_prefix == ()


# ── Décision ───────────────────────────────────────────────────────────────────

def test_rien_a_proposer_quand_postgres_repond():
    assert decide_action(ServerState(psql=True, reachable=True)) == ACTION_NONE


def test_client_absent_propose_installation():
    state = ServerState(psql=False, reachable=False, package_manager="apt", can_admin=True)

    assert decide_action(state) == ACTION_INSTALL


def test_serveur_muet_propose_demarrage_pas_installation():
    """Démarrer un service déjà installé est bien moins invasif : la question diffère."""
    state = ServerState(psql=True, reachable=False, package_manager="dnf",
                        can_admin=True, have_systemctl=True)

    assert decide_action(state) == ACTION_START
    # Ni systemctl ni service : rien à proposer plutôt qu'une commande qui n'existe pas.
    assert decide_action(ServerState(psql=True, reachable=False, can_admin=True)) == ACTION_IMPOSSIBLE


def test_sans_droits_aucune_offre():
    state = ServerState(psql=False, reachable=False, package_manager="apt", can_admin=False)

    assert decide_action(state) == ACTION_IMPOSSIBLE


def test_sans_gestionnaire_connu_aucune_offre():
    state = ServerState(psql=False, reachable=False, package_manager="", can_admin=True)

    assert decide_action(state) == ACTION_IMPOSSIBLE


# ── Commandes ──────────────────────────────────────────────────────────────────

def test_commandes_debian_ferment_stdin_et_retentent_apres_update():
    state = ServerState(psql=False, reachable=False, package_manager="apt",
                        can_admin=True, have_systemctl=True, sudo_prefix=("sudo",))

    steps = plan_steps(state, ACTION_INSTALL)

    assert steps[0].argv == ("sudo", "env", "DEBIAN_FRONTEND=noninteractive",
                             "apt-get", "install", "-y", "postgresql")
    # Cache apt jamais rafraîchi (machine fraîche) : un seul nouvel essai après update.
    assert steps[0].recover == ("sudo", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "update")
    assert steps[-1].argv == ("sudo", "systemctl", "enable", "--now", "postgresql")


def test_commandes_rhel_initdb_avant_le_service():
    """Sans `postgresql-setup --initdb`, le service RHEL refuse de démarrer."""
    state = ServerState(psql=False, reachable=False, package_manager="dnf",
                        can_admin=True, have_systemctl=True)

    argvs = [step.argv for step in plan_steps(state, ACTION_INSTALL)]

    assert argvs[0] == ("dnf", "install", "-y", "postgresql-server", "postgresql")
    assert argvs[1] == ("postgresql-setup", "--initdb")
    assert argvs[2] == ("systemctl", "enable", "--now", "postgresql")
    # Cluster déjà initialisé = commande en erreur légitime : elle ne doit rien casser.
    assert plan_steps(state, ACTION_INSTALL)[1].tolerate_failure is True


def test_systemctl_present_mais_sans_systemd_retombe_sur_service():
    """Trouvé au rejeu réel (conteneur Ubuntu où ffmpeg tire systemd sans PID 1) :
    `systemctl` existe, sort en « Failed to connect to bus », et le serveur reste muet."""
    state = ServerState(psql=True, reachable=False, package_manager="apt", can_admin=True,
                        have_systemctl=True, have_service=True)
    step = plan_steps(state, ACTION_START)[-1]

    assert step.argv == ("systemctl", "enable", "--now", "postgresql")
    assert step.fallback == ("service", "postgresql", "start")

    joues: list[tuple[str, ...]] = []

    def _run(cmd):
        joues.append(tuple(cmd))
        return 1 if "systemctl" in cmd[0] else 0

    ensure_server(
        console=Console(io.StringIO(), color=False),
        state=state,
        run=_run,
        which=_which("psql", "pg_isready", "apt-get", "systemctl", "service"),
        is_root=True,
        os_release=Path("/etc/os-release"),
    )

    assert ("service", "postgresql", "start") in joues


def test_demarrage_sans_systemctl_retombe_sur_service():
    state = ServerState(psql=True, reachable=False, package_manager="apt",
                        can_admin=True, have_service=True)

    assert plan_steps(state, ACTION_START)[-1].argv == ("service", "postgresql", "start")


def test_sans_systemctl_ni_service_on_n_invente_pas_de_commande(tmp_path):
    """Trouvé au rejeu réel (conteneur fedora:42, ni systemd ni `service`) : appeler une
    commande absente levait FileNotFoundError et sortait en traceback."""
    state = ServerState(psql=False, reachable=False, package_manager="dnf", can_admin=True)

    argvs = [step.argv for step in plan_steps(state, ACTION_INSTALL)]
    assert argvs == [("dnf", "install", "-y", "postgresql-server", "postgresql"),
                     ("postgresql-setup", "--initdb")]

    stream = io.StringIO()
    ok = ensure_server(
        console=Console(stream, color=False),
        state=state,
        # Runner réel : le binaire manquant remonte en échec d'étape, jamais en exception.
        which=_which("dnf"),
        is_root=True,
        os_release=_os_release(tmp_path, "ID=fedora\n"),
    )

    assert ok is False
    assert "toujours indisponible" in stream.getvalue()


# ── Exécution + re-sonde ───────────────────────────────────────────────────────

def test_le_verdict_vient_de_la_sonde_pas_du_gestionnaire(tmp_path):
    """apt sort en 0 mais aucun serveur ne répond : on ne dit PAS que c'est prêt."""
    stream = io.StringIO()
    state = ServerState(psql=False, reachable=False, package_manager="apt",
                        can_admin=True, have_systemctl=True)

    ok = ensure_server(
        console=Console(stream, color=False),
        state=state,
        run=lambda cmd: 0,                       # tout « réussit »…
        which=_which("apt-get", "systemctl"),    # …mais psql reste absent à l'arrivée
        is_root=True,
        os_release=_os_release(tmp_path, "ID=debian\n"),
    )

    assert ok is False
    assert "toujours indisponible" in stream.getvalue()


def test_installation_reussie_est_annoncee(tmp_path):
    stream = io.StringIO()
    state = ServerState(psql=False, reachable=False, package_manager="apt",
                        can_admin=True, have_systemctl=True)

    ok = ensure_server(
        console=Console(stream, color=False),
        state=state,
        run=lambda cmd: 0,
        which=_which("psql", "apt-get", "systemctl"),  # psql présent après installation
        is_root=True,
        os_release=_os_release(tmp_path, "ID=debian\n"),
    )

    assert ok is True
    assert "PostgreSQL disponible" in stream.getvalue()


def test_rien_a_faire_ne_lance_aucune_commande(tmp_path):
    ok = ensure_server(
        console=Console(io.StringIO(), color=False),
        state=ServerState(psql=True, reachable=True),
        run=lambda cmd: pytest.fail("aucune commande ne doit être jouée"),
        which=_which("psql"),
        is_root=True,
        os_release=_os_release(tmp_path, "ID=debian\n"),
    )

    assert ok is True


# ── Rendus ─────────────────────────────────────────────────────────────────────

def test_render_probe_shell_est_filtrable():
    state = ServerState(psql=True, reachable=False, package_manager="dnf")

    assert render_probe_shell(state, ACTION_START) == (
        "PG_SERVER_ACTION=start\n"
        "PG_SERVER_PSQL=true\n"
        "PG_SERVER_REACHABLE=false\n"
        "PG_SERVER_MANAGER=dnf"
    )


def test_render_setup_log_refuse_un_evenement_inconnu():
    with pytest.raises(ValueError, match="événement serveur PostgreSQL inconnu"):
        render_setup_log(event="inventé")

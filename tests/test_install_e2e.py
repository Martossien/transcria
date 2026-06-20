"""E2E d'install par profil : exécute *réellement* `install.sh` dans un bac à sable.

Contrairement à `test_install_script.py` (contrats `--plan` sans effet de bord), ces
tests exercent le **vrai chaînage shell → Python** de bout en bout : génération de
`config.yaml`/`.env`, écriture du DSN, `alembic upgrade`, puis la barrière
`doctor --profile`. C'est le « filet d'intégration » qui prouve que les briques
unitaires, une fois assemblées par `install.sh`, produisent un déploiement cohérent.

Les tests sont volontairement **bornés** : les étapes lourdes ou privilégiées sont
désactivées par des primitives conçues pour ça (et alignées Docker) —

  * ``--skip-deps``   : le venv est réutilisé (jamais de `pip` réseau ni de venv neuf) ;
  * ``--no-service``  : aucun systemd ;
  * ``--pg-existing`` : la base est déjà provisionnée (ici par la fixture éphémère),
                        donc pas de bootstrap rôle/base via `sudo postgres`.

Ce même harnais (sandbox + `install.sh --profile X` + assert doctor) est le socle
prévu pour les futurs smokes de **build Docker** : mêmes profils, base PostgreSQL
externe, dépendances installées en couche build. Garder `_build_sandbox` /
`_run_install` réutilisables et paramétrés par profil est délibéré.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from pytest_postgresql.janitor import DatabaseJanitor

_REPO = Path(__file__).resolve().parent.parent
_INSTALL = _REPO / "install.sh"

# Entrées du dépôt que `install.sh` lit (jamais n'écrit) : symlinkées pour ne pas
# recopier le venv (lourd) ni le package. Tout le reste est COPIÉ, car l'install y
# écrit (scripts/generated/, configs/local/, …) et on refuse toute fuite vers le repo.
_SYMLINKED = ("venv", "transcria", "inference_service")
_COPIED = (
    "scripts",
    "configs",
    "alembic",
    "alembic.ini",
    "requirements.txt",
    ".env.example",
    "config.example.yaml",
    "app.py",
    "wsgi.py",
)

# Profils testés E2E : ceux qui produisent un déploiement applicatif validable par
# doctor sans GPU. `resource-node` (GPU + clés) et `all-in-one` (legacy, GPU) sont
# couverts ailleurs / à venir.
_PROFILES = ("web", "scheduler")

_TIMEOUT_S = 240


def _require_install_prereqs() -> None:
    if not _INSTALL.exists():
        pytest.skip("install.sh introuvable")
    if not (_REPO / "venv" / "bin" / "python").exists():
        pytest.skip("venv du dépôt requis pour --skip-deps (E2E réutilise les dépendances)")


def _build_sandbox(dest: Path) -> Path:
    """Construit un répertoire d'installation isolé.

    Lecture seule (venv, package) → symlink. Cibles d'écriture (scripts, configs, …)
    → copie, pour qu'aucune écriture de l'install ne remonte dans le dépôt versionné.
    `config.yaml` et `.env` n'existent pas : ils sont produits par l'install.
    """
    dest.mkdir(parents=True, exist_ok=True)
    for name in _SYMLINKED:
        src = _REPO / name
        if src.exists():
            (dest / name).symlink_to(src, target_is_directory=src.is_dir())
    for name in _COPIED:
        src = _REPO / name
        if not src.exists():
            continue
        if src.is_dir():
            shutil.copytree(src, dest / name, symlinks=True)
        else:
            shutil.copy2(src, dest / name)
    return dest


def _run_install(sandbox: Path, profile: str, pg: "PgParams", *extra: str) -> subprocess.CompletedProcess[str]:
    """Lance `install.sh` en non-interactif, borné, contre la base éphémère existante."""
    cmd = [
        "bash",
        str(_INSTALL),
        "--install-dir", str(sandbox),
        "--profile", profile,
        "--non-interactive",
        "--skip-deps",
        "--no-service",
        "--postgres",
        "--pg-existing",
        "--pg-host", pg.host,
        "--pg-port", str(pg.port),
        "--pg-db", pg.dbname,
        "--pg-user", pg.user,
        "--pg-password", pg.password,
        *extra,
    ]
    # HOME pointe vers le bac à sable : la phase opencode (OPENCODE_HOME=$HOME) écrit
    # alors sa config provider dans le sandbox, jamais dans le ~/.config/opencode réel.
    env = {**os.environ, "TRANSCRIA_CONFIG": str(sandbox / "config.yaml"), "HOME": str(sandbox)}
    env.pop("TRANSCRIA_DATABASE_URL", None)  # ne pas masquer ce que l'install écrit
    return subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT_S, cwd=str(sandbox), env=env)


def _run_doctor(sandbox: Path, profile: str, pg: "PgParams") -> subprocess.CompletedProcess[str]:
    """Rejoue la barrière doctor du profil contre l'install assemblée."""
    cmd = [
        str(sandbox / "venv" / "bin" / "python"),
        str(sandbox / "scripts" / "doctor.py"),
        "--config", str(sandbox / "config.yaml"),
        "--profile", profile,
    ]
    env = {**os.environ, "ENV_FILE": str(sandbox / ".env"), "TRANSCRIA_DATABASE_URL": pg.url}
    return subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT_S, cwd=str(sandbox), env=env)


class PgParams:
    """Paramètres de connexion vers la base éphémère dédiée à un test."""

    __slots__ = ("host", "port", "user", "password", "dbname")

    def __init__(self, host: str, port: int, user: str, password: str, dbname: str) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.dbname = dbname

    @property
    def url(self) -> str:
        auth = self.user if not self.password else f"{self.user}:{self.password}"
        return f"postgresql+psycopg://{auth}@{self.host}:{self.port}/{self.dbname}"


@pytest.fixture
def pg_params(postgresql_proc):
    """Crée une base PostgreSQL jetable et dédiée (rôle = superuser du cluster éphémère).

    `--pg-existing` suppose rôle + base déjà créés : c'est exactement le cas ici, et le
    modèle d'un PostgreSQL fourni par un conteneur/service externe en Docker.
    """
    dbname = f"transcria_e2e_{uuid.uuid4().hex[:12]}"
    with DatabaseJanitor(
        user=postgresql_proc.user,
        host=postgresql_proc.host,
        port=postgresql_proc.port,
        version=postgresql_proc.version,
        dbname=dbname,
        password=postgresql_proc.password,
    ):
        yield PgParams(
            host=postgresql_proc.host,
            port=int(postgresql_proc.port),
            user=postgresql_proc.user,
            password=postgresql_proc.password or "",
            dbname=dbname,
        )


def _repo_status() -> set[str]:
    """Empreinte de l'état du dépôt versionné (porcelaine).

    On compare *avant/après* l'install plutôt que d'exiger un arbre propre : le
    chantier en cours dirtie légitimement l'arbre. Seule une entrée *nouvelle*
    trahit une écriture qui a fui vers le dépôt via les symlinks.
    """
    out = subprocess.run(
        ["git", "-C", str(_REPO), "status", "--porcelain"],
        capture_output=True, text=True, timeout=30,
    )
    return {line for line in out.stdout.splitlines() if line.strip()}


@pytest.mark.parametrize("profile", _PROFILES)
def test_install_profile_e2e(profile: str, pg_params: PgParams, tmp_path: Path):
    """`install.sh --profile X` produit une install cohérente que doctor valide."""
    _require_install_prereqs()
    sandbox = _build_sandbox(tmp_path / "sandbox")
    repo_before = _repo_status()

    result = _run_install(sandbox, profile, pg_params)
    assert result.returncode == 0, (
        f"install.sh --profile {profile} a échoué (code {result.returncode})\n"
        f"--- stdout ---\n{result.stdout[-4000:]}\n--- stderr ---\n{result.stderr[-2000:]}"
    )

    # Artefacts générés, locaux au sandbox (jamais dans le dépôt).
    config_yaml = sandbox / "config.yaml"
    env_file = sandbox / ".env"
    assert config_yaml.is_file(), "config.yaml non généré"
    assert env_file.is_file(), ".env non généré"
    assert pg_params.dbname in env_file.read_text(), "DSN PostgreSQL absent de .env"

    # La barrière doctor du profil doit passer (warnings tolérés, échec bloquant interdit).
    doctor = _run_doctor(sandbox, profile, pg_params)
    assert doctor.returncode == 0, (
        f"doctor --profile {profile} a signalé un échec bloquant (code {doctor.returncode})\n"
        f"--- stdout ---\n{doctor.stdout[-4000:]}\n--- stderr ---\n{doctor.stderr[-2000:]}"
    )

    # Aucune écriture ne doit avoir fui vers le dépôt via les symlinks.
    leaked = _repo_status() - repo_before
    assert not leaked, f"l'install a modifié des fichiers versionnés : {sorted(leaked)}"

"""Sondes EFFECTIVES du doctor (réseau, disque, systemd, base) — injectables dans les checks.

Chaque vérification reçoit sa sonde en kwarg avec l'une de celles-ci pour défaut : les tests
injectent des sondes factices, la prod utilise celles-ci. Aucune logique de décision ici —
uniquement l'accès au monde extérieur, isolé pour rester remplaçable.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable


def _probe_server_encoding(uri: str) -> str:
    """Sonde l'encodage serveur de la base PostgreSQL, hors process Flask."""
    from sqlalchemy import create_engine

    engine = create_engine(uri)
    try:
        with engine.connect() as conn:
            return str(conn.exec_driver_sql("SHOW server_encoding").scalar())
    finally:
        engine.dispose()

def _systemd_unit_state(unit: str) -> tuple[bool, bool] | None:
    """Retourne (active, enabled) ou None si systemd n'est pas utilisable ici."""
    if not shutil.which("systemctl"):
        return None

    def _run(*args: str) -> int:
        try:
            return subprocess.run(
                ["systemctl", *args, unit],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            ).returncode
        except (OSError, subprocess.SubprocessError):
            return 4

    active_rc = _run("is-active", "--quiet")
    enabled_rc = _run("is-enabled", "--quiet")
    if active_rc == 4 and enabled_rc == 4:
        return None
    return active_rc == 0, enabled_rc == 0

def _job_files_table_exists(uri: str) -> bool:
    """Sonde l'existence de la table `job_files` (backend pg), hors process Flask."""
    from sqlalchemy import create_engine, inspect

    engine = create_engine(uri)
    try:
        return bool(inspect(engine).has_table("job_files"))
    finally:
        engine.dispose()

def _probe_openai_models(port: int, timeout: int = 3) -> dict | None:
    """GET /v1/models sur le port local ; retourne le JSON ou None si injoignable."""
    import requests

    try:
        resp = requests.get(f"http://127.0.0.1:{port}/v1/models", timeout=timeout)
        if resp.status_code == 200:
            return resp.json()
    except Exception:  # noqa: BLE001
        return None
    return None

def _probe_node_health(url: str, timeout: int = 3) -> bool:
    import requests

    try:
        return requests.get(f"{url.rstrip('/')}/health", timeout=timeout).status_code == 200
    except Exception:  # noqa: BLE001
        return False

def _safe_health(health: Callable[[str], bool], url: str) -> bool:
    try:
        return bool(health(url))
    except Exception:  # noqa: BLE001
        return False

def _probe_node_capabilities(url: str, timeout: int = 3) -> dict | None:
    import requests

    try:
        resp = requests.get(f"{url.rstrip('/')}/capabilities", timeout=timeout)
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None

def _safe_capabilities(probe: Callable[[str], dict | None], url: str) -> dict | None:
    try:
        result = probe(url)
        return result if isinstance(result, dict) else None
    except Exception:  # noqa: BLE001
        return None

def _dir_writable(path: str) -> bool:
    """True si on peut écrire dans ``path`` (ou, s'il n'existe pas, dans son parent)."""
    if os.path.isdir(path):
        return os.access(path, os.W_OK)
    parent = os.path.dirname(os.path.abspath(path))
    return os.path.isdir(parent) and os.access(parent, os.W_OK)

def _tcp_port_open(port: int, *, host: str = "127.0.0.1", timeout: float = 0.2) -> bool:
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False

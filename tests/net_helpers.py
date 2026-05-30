"""Utilitaires partagés par les tests d'intégration réseau (vrai socket TCP).

Pas un module de test (non collecté par pytest) : factorise le démarrage d'un
serveur Flask en thread, l'attribution de ports libres et la détection d'IP LAN.
"""
from __future__ import annotations

import contextlib
import socket
import threading
import time

import requests
from werkzeug.serving import make_server


def free_port(host: str = "127.0.0.1") -> int:
    """Un port TCP libre sur `host` (libéré immédiatement, à réutiliser vite)."""
    with socket.socket() as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def primary_lan_ip() -> str | None:
    """IPv4 LAN principale (sans émettre de paquet), ou None si seulement loopback."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None


@contextlib.contextmanager
def serve_flask(app, host: str, port: int, *, ready_path: str = "/health", timeout: float = 5.0):
    """Sert `app` sur (host, port) dans un thread, attend la readiness, puis nettoie.

    `ready_path` est sondé en GET jusqu'à une 200 (ou < 500) avant de rendre la main.
    Yield l'URL racine `http://host:port`.
    """
    srv = make_server(host, port, app, threaded=True)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://{host}:{port}"
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if requests.get(f"{base}{ready_path}", timeout=1).status_code < 500:
                    break
            except requests.RequestException:
                time.sleep(0.05)
        yield base
    finally:
        srv.shutdown()
        thread.join(timeout=5)

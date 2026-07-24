"""Point d'entrée du service connecteur : `python -m connector_service`.

Charge la config (YAML/JSON) depuis `TRANSCRIA_CONNECTOR_CONFIG`, monte l'app des
plateformes activées, et sert. En prod, un serveur WSGI (gunicorn) sert `app` ; ce runner
est le lancement simple (dev / unité systemd minimale).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from connector_service.app import create_connector_app


def load_config(path: str) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    try:
        import yaml
        data = yaml.safe_load(text)
    except Exception:  # noqa: BLE001 — pas de yaml / pas du YAML → tenter JSON
        data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("config connecteur invalide (objet attendu)")
    return data


def main(argv: list[str] | None = None) -> int:
    config_path = os.environ.get("TRANSCRIA_CONNECTOR_CONFIG", "")
    if not config_path:
        print("TRANSCRIA_CONNECTOR_CONFIG manquant (chemin de la config du connecteur)",
              file=sys.stderr)
        return 2
    config = load_config(config_path)
    app = create_connector_app(config)
    server = config.get("server") or {}
    app.run(host=str(server.get("host", "127.0.0.1")), port=int(server.get("port", 7880)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

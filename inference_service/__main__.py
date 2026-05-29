"""Point d'entrée du service d'inférence.

    python -m inference_service                  # 127.0.0.1:8002 (dev)
    INFERENCE_HOST=0.0.0.0 INFERENCE_PORT=8002 python -m inference_service

En production, servir via un serveur WSGI (gunicorn) :
    gunicorn "inference_service:create_app()" -b 127.0.0.1:8002 --workers 1

⚠ --workers 1 : le moteur charge un modèle GPU résident, plusieurs workers
multiplieraient la VRAM. La concurrence GPU est déjà sérialisée par le verrou
interne du moteur (un calcul à la fois).
"""
from __future__ import annotations

import os

from inference_service.app import create_app


def main() -> None:
    host = os.environ.get("INFERENCE_HOST", "127.0.0.1")
    port = int(os.environ.get("INFERENCE_PORT", "8002"))
    app = create_app()
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()

"""Point d'entrée du bot : `python -m connector_service.bot <url>`.

C'est l'ENTRYPOINT de l'image Docker (un conteneur = une réunion).
"""
from __future__ import annotations

import sys

from connector_service.bot.cli import main

if __name__ == "__main__":
    sys.exit(main())

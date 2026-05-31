"""Point d'entrée WSGI pour gunicorn (Phase B / C1 — tier web scalable).

Exemple (tier web, sans état, N workers) :

    TRANSCRIA_ROLE=web gunicorn --workers 4 --bind 0.0.0.0:7870 \
        --timeout 120 --access-logfile - wsgi:app

Le rôle est lu via ``TRANSCRIA_ROLE`` (défaut ``all``). En ``web``, ``create_app``
ne démarre PAS l'ordonnanceur de file : les workers gunicorn ne touchent ni au GPU
ni à la file (ils peuvent seulement enfiler). Un process ``--role scheduler`` unique
draine la file en parallèle (`python app.py --role scheduler`).

⚠️ N'utilisez PAS gunicorn pour le rôle ``scheduler`` (ce serait un serveur HTTP, pas
l'orchestrateur) ni avec ``--workers > 1`` pour le rôle ``all`` (dupliquerait
l'orchestrateur ; le verrou consultatif l'empêcherait de drainer mais c'est inutile).
"""
from __future__ import annotations

from app import create_app

app = create_app()

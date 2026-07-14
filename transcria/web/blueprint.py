"""Blueprint unique `web` partagé par tous les modules de routes (vague A2).

Un seul ``Blueprint("web", …)`` pour tout le paquet : les endpoints restent
``web.xxx`` et les ``url_for('web.…')`` des templates survivent au découpage.
Chaque module de routes importe ``web_bp`` d'ici et y accroche les siennes ;
``app.py`` n'enregistre toujours qu'un blueprint.
"""
from flask import Blueprint

web_bp = Blueprint("web", __name__)

"""Réglages HTTP communs aux appels de plateforme (Zoom, Teams, Meet, OAuth).

Un délai d'attente codé en dur transforme un proxy lent ou une plateforme chargée en échec
sans recours. Valeur NOMMÉE et surchargeable par appel — les connecteurs traversent souvent
un proxy d'entreprise, où 30 s peut être court.
"""
from __future__ import annotations

# Délai par défaut des appels HTTP sortants vers les plateformes (secondes).
DEFAULT_HTTP_TIMEOUT_S: float = 30.0

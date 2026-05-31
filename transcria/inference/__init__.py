"""Client du service d'inférence distant (côté frontend TranscrIA).

Permet au pipeline d'appeler le `inference_service` (diarisation, embeddings
voix) au lieu de charger les modèles en process. Voir
docs/MIGRATION_API_SERVEUR_GPU.md.
"""
from transcria.inference.client import (
    FailoverInferenceClient,
    InferenceClient,
    InferenceRequestError,
    InferenceUnavailable,
    build_client_from_config,
)

__all__ = [
    "InferenceClient",
    "FailoverInferenceClient",
    "InferenceUnavailable",
    "InferenceRequestError",
    "build_client_from_config",
]

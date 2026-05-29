"""TranscrIA Inference Service — service d'inférence dédié (Phase 0).

Héberge les composants qui n'ont aucun standard API (diarisation, embeddings
voix) derrière une API HTTP, pour découpler le plan de calcul GPU du frontend.
Tourne d'abord en localhost (127.0.0.1), déménageable en distant sans changer
le contrat. Voir docs/MIGRATION_API_SERVEUR_GPU.md (§4bis).
"""
from inference_service.app import create_app

__all__ = ["create_app"]

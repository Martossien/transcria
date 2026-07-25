"""Passerelle LIVE (L0) — plan de données audio temps réel, ISOLÉ (aucun import du cœur).

Consomme des `AudioFrame` (d'un `LiveMediaProvider`), les fait passer par une chaîne STT
live, produit des segments à provenance progressive (`partial`→`provisional`→`final_live`),
et à la fin de réunion déverse vers l'API de jobs TranscrIA (segments `canonical` produits
par le pipeline batch). Voir docs/adr/ADR-001 (D5/D6).
"""

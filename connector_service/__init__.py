"""Service connecteur de réunion — ASYNC, ISOLÉ, opt-in (A0, temps réel).

Process séparé du web sync de TranscrIA (patron `inference_service`). Il capte les
artefacts/flux des plateformes (Visio/Zoom/Teams/Meet) et les déverse dans TranscrIA
**par son API de jobs HTTP** — jamais par import du cœur. Un contrat import-linter
garantit que `transcria`/`inference_service` n'importent PAS `connector_service`.

Voir docs/adr/ADR-001-frontiere-ingestion-reunions.md (D3 contrat par capacités,
D4 séparation contrôle/données).
"""

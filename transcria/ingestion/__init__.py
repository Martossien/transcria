"""Ingestion des artefacts de réunion externes (A0, temps réel & connecteurs).

Le cœur de l'idempotence côté serveur : `MeetingImport` relie un artefact distant
(identifié par une `dedup_key` non-nulle) à un job TranscrIA, sous contrainte
`UNIQUE(dedup_key)`. Un connecteur qui rejoue un webhook — ou deux webhooks
simultanés — n'obtient qu'un seul job. Voir docs/adr/ADR-001-frontiere-ingestion-reunions.md (D2).
"""

"""Réconciliation des artefacts manqués (A0 — ADR-001 D2-bis).

Les webhooks seuls ne garantissent pas « 0 perte silencieuse ». Le réconciliateur
liste les artefacts RÉELS d'une plateforme (via un `ArtifactProvider`), les compare aux
déjà-importés, et importe le manquant par le pont — **sans doublon**. Sûr par
construction : même si son ensemble local est périmé, l'`Idempotency-Key` côté serveur
(MeetingImport) empêche tout second job. Il est donc rejouable à volonté.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from connector_service.bridge import IngestResult, JobsApiBridge
from connector_service.contract import (
    ArtifactProvider,
    ExternalMeetingOccurrence,
    RemoteArtifact,
)


def default_dedup_key(occurrence: ExternalMeetingOccurrence, artifact: RemoteArtifact) -> str:
    """Clé composite (ADR-001 D2), envoyée en `Idempotency-Key` : jamais `external_meeting_id`
    seul (réutilisé par les réunions récurrentes)."""
    return "|".join((
        occurrence.provider,
        occurrence.provider_account_id,
        occurrence.external_occurrence_id,
        artifact.artifact_id,
    ))


@dataclass(frozen=True)
class ReconcileOutcome:
    dedup_key: str
    action: str            # "imported" | "skipped_known"
    result: IngestResult | None = None


class ProviderReconciler:
    def __init__(
        self,
        provider: ArtifactProvider,
        bridge: JobsApiBridge,
        *,
        fetch_audio: Callable[[RemoteArtifact], Awaitable[tuple[bytes, str]]],
        key_of: Callable[[ExternalMeetingOccurrence, RemoteArtifact], str] = default_dedup_key,
    ) -> None:
        self._provider = provider
        self._bridge = bridge
        self._fetch_audio = fetch_audio
        self._key_of = key_of

    async def reconcile(
        self,
        occurrence: ExternalMeetingOccurrence,
        *,
        already_imported: set[str] | None = None,
    ) -> list[ReconcileOutcome]:
        """Pour chaque artefact : si sa clé est déjà connue localement → sauté (pas de
        re-téléchargement) ; sinon → téléchargé + ingéré (le serveur reste le garde ultime
        d'idempotence). Met à jour `already_imported` au fil de l'eau."""
        seen = already_imported if already_imported is not None else set()
        outcomes: list[ReconcileOutcome] = []
        for artifact in await self._provider.fetch_artifacts(occurrence):
            key = self._key_of(occurrence, artifact)
            if key in seen:
                outcomes.append(ReconcileOutcome(dedup_key=key, action="skipped_known"))
                continue
            audio, filename = await self._fetch_audio(artifact)
            result = await self._bridge.ingest_recording(
                audio, filename, idempotency_key=key,
                provider=occurrence.provider,
                external_meeting_id=occurrence.external_occurrence_id,
            )
            seen.add(key)
            outcomes.append(ReconcileOutcome(dedup_key=key, action="imported", result=result))
        return outcomes

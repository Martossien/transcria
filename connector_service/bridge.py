"""Pont vers l'API de jobs TranscrIA (A0 — ADR-001 D2/§4).

Le connecteur ne touche JAMAIS le cœur : il POSTe l'enregistrement à
``/v1/audio/ingest`` par HTTP, authentifié par jeton `tia_` (Bearer), avec un en-tête
``Idempotency-Key`` (la clé composite provider+compte+occurrence+artefact du connecteur)
→ un rejeu renvoie le MÊME job côté serveur.

Le transport HTTP est **injecté** (Protocol `Transport`) : pas de dépendance dure à un
client HTTP dans le contrat, et les tests passent un transport factice (zéro réseau).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class Transport(Protocol):
    """Abstraction HTTP minimale. Une implémentation réelle enveloppe httpx/aiohttp ;
    les tests en fournissent une factice."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        data: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes]] | None = None,
    ) -> tuple[int, dict]: ...


@dataclass(frozen=True)
class IngestResult:
    status_code: int
    job_id: str | None
    idempotent: bool


class JobsApiBridge:
    """Client de l'API de jobs TranscrIA pour le service connecteur."""

    def __init__(self, base_url: str, api_token: str, transport: Transport) -> None:
        self._base = base_url.rstrip("/")
        self._token = api_token
        self._transport = transport

    async def ingest_recording(
        self,
        audio: bytes,
        filename: str,
        *,
        idempotency_key: str,
        provider: str | None = None,
        external_meeting_id: str | None = None,
    ) -> IngestResult:
        """POST /v1/audio/ingest. `idempotency_key` porte l'idempotence côté serveur :
        deux appels avec la même clé ⇒ un seul job (le 2e revient `idempotent=True`)."""
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Idempotency-Key": idempotency_key,
        }
        data: dict[str, str] = {}
        if provider:
            data["provider"] = provider
        if external_meeting_id:
            data["external_meeting_id"] = external_meeting_id
        status, body = await self._transport.request(
            "POST", f"{self._base}/v1/audio/ingest",
            headers=headers, data=data, files={"file": (filename, audio)},
        )
        return IngestResult(
            status_code=status,
            job_id=body.get("job_id"),
            idempotent=bool(body.get("idempotent", False)),
        )

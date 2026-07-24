"""A1 lot 1 — adaptateur de tâche Visio, contre une fixture à la forme réelle.

La fixture reprend les 7 params documentés de `suitenumerique/meet`. L'enveloppe JSON
exacte reste à confirmer contre une instance réelle (gate E2E manuel) — d'où le parsing
tolérant, exercé ici sur les cas valide / champ manquant / doublon.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from connector_service.bridge import JobsApiBridge
from connector_service.providers.visio import (
    VisioIngestHandler,
    VisioTask,
    VisioTaskAdapter,
    VisioTaskError,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "visio_task.json").read_text())


def test_from_payload_fixture_valide():
    task = VisioTask.from_payload(FIXTURE)
    assert task.room == "room-alpha" and task.sub == "oidc|alice-9f2c"
    assert task.filename.endswith(".mp3")


def test_from_payload_champ_manquant_erreur():
    bad = dict(FIXTURE)
    del bad["filename"]
    with pytest.raises(VisioTaskError, match="filename"):
        VisioTask.from_payload(bad)


def test_from_payload_champ_vide_erreur():
    bad = dict(FIXTURE, room="  ")
    with pytest.raises(VisioTaskError, match="room"):
        VisioTask.from_payload(bad)


def test_from_payload_champs_en_trop_toleres():
    task = VisioTask.from_payload(dict(FIXTURE, extra="ignoré", meeting_id="x"))
    assert task.room == "room-alpha"


def test_from_payload_non_dict_erreur():
    with pytest.raises(VisioTaskError):
        VisioTask.from_payload("pas un objet")  # type: ignore[arg-type]


class TestAdapter:
    adapter = VisioTaskAdapter(bucket="visio-recordings")

    def test_occurrence(self):
        occ = self.adapter.to_occurrence(VisioTask.from_payload(FIXTURE))
        assert occ.provider == "visio"
        assert occ.provider_account_id == "oidc|alice-9f2c"
        assert occ.external_occurrence_id == "room-alpha:2026-07-24:14:30:00"
        assert occ.organizer == "alice@example.org"

    def test_artifact_pointe_minio(self):
        art = self.adapter.to_artifact(VisioTask.from_payload(FIXTURE))
        assert art.artifact_type == "recording"
        assert art.artifact_id == "recordings/room-alpha/2026-07-24-1430.mp3"
        assert art.storage_uri == "s3://visio-recordings/recordings/room-alpha/2026-07-24-1430.mp3"

    def test_dedup_key_composite_et_deterministe(self):
        task = VisioTask.from_payload(FIXTURE)
        k = self.adapter.dedup_key(task)
        assert k == self.adapter.dedup_key(task)                       # déterministe
        assert k.startswith("visio|oidc|alice-9f2c|room-alpha:")       # composite
        assert task.filename in k                                       # inclut l'artefact

    def test_dedup_distingue_les_occurrences_recurrentes(self):
        # Même SALLE réutilisée un autre jour → clé DIFFÉRENTE (jamais la salle seule).
        t1 = VisioTask.from_payload(FIXTURE)
        t2 = VisioTask.from_payload(dict(FIXTURE, recording_date="2026-07-25",
                                         filename="recordings/room-alpha/2026-07-25-1430.mp3"))
        assert self.adapter.dedup_key(t1) != self.adapter.dedup_key(t2)


class _FakeFetcher:
    def __init__(self):
        self.fetched: list = []

    async def fetch(self, artifact):
        self.fetched.append(artifact.storage_uri)
        return b"VISIO-AUDIO", artifact.artifact_id.rsplit("/", 1)[-1]


class _FakeServerTransport:
    """Mime /v1/audio/ingest : une Idempotency-Key connue ⇒ même job (200 idempotent)."""

    def __init__(self):
        self._by_key: dict[str, str] = {}

    async def request(self, method, url, *, headers, data=None, files=None):
        key = headers["Idempotency-Key"]
        if key in self._by_key:
            return 200, {"job_id": self._by_key[key], "idempotent": True}
        job_id = f"job-{len(self._by_key) + 1}"
        self._by_key[key] = job_id
        return 202, {"job_id": job_id}


class TestIngestHandler:
    def _handler(self, fetcher, transport):
        return VisioIngestHandler(
            VisioTaskAdapter(bucket="visio-recordings"),
            fetcher,
            JobsApiBridge("http://127.0.0.1:7870", "tia_x", transport),
        )

    def test_handle_tache_valide_ingere(self):
        fetcher, tr = _FakeFetcher(), _FakeServerTransport()
        res = asyncio.run(self._handler(fetcher, tr).handle(FIXTURE))
        assert res.status_code == 202 and res.job_id == "job-1" and res.idempotent is False
        assert fetcher.fetched == [
            "s3://visio-recordings/recordings/room-alpha/2026-07-24-1430.mp3"]

    def test_handle_tache_rejouee_ne_double_pas(self):
        tr = _FakeServerTransport()
        r1 = asyncio.run(self._handler(_FakeFetcher(), tr).handle(FIXTURE))
        r2 = asyncio.run(self._handler(_FakeFetcher(), tr).handle(FIXTURE))  # rejeu
        assert r1.job_id == r2.job_id == "job-1" and r2.idempotent is True

    def test_handle_tache_invalide_leve(self):
        bad = dict(FIXTURE)
        del bad["sub"]
        with pytest.raises(VisioTaskError, match="sub"):
            asyncio.run(self._handler(_FakeFetcher(), _FakeServerTransport()).handle(bad))

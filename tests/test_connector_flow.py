"""A0 lot 3 — réconciliation + service async (transport factice mimant l'idempotence
serveur : même Idempotency-Key ⇒ même job, jamais un second)."""
from __future__ import annotations

import asyncio

from connector_service.bridge import JobsApiBridge
from connector_service.contract import ExternalMeetingOccurrence as Occ
from connector_service.fakes import FakePostMeetingProvider
from connector_service.reconciler import ProviderReconciler
from connector_service.service import ConnectorService

OCC = Occ(provider="visio", provider_account_id="acct", external_occurrence_id="occ-1")


class _FakeServerTransport:
    """Mime /v1/audio/ingest : une clé d'idempotence connue ⇒ même job (200 idempotent)."""

    def __init__(self):
        self.calls = 0
        self._by_key: dict[str, str] = {}

    async def request(self, method, url, *, headers, data=None, files=None):
        self.calls += 1
        key = headers["Idempotency-Key"]
        if key in self._by_key:
            return 200, {"job_id": self._by_key[key], "idempotent": True}
        job_id = f"job-{len(self._by_key) + 1}"
        self._by_key[key] = job_id
        return 202, {"job_id": job_id}


async def _fetch_audio(artifact):
    return b"AUDIO-BYTES", artifact.artifact_id + ".mp4"


def _reconciler(transport):
    bridge = JobsApiBridge("http://127.0.0.1:7870", "tia_x", transport)
    return ProviderReconciler(FakePostMeetingProvider(), bridge, fetch_audio=_fetch_audio)


def test_reconcile_importe_le_manquant():
    tr = _FakeServerTransport()
    out = asyncio.run(_reconciler(tr).reconcile(OCC, already_imported=set()))
    assert len(out) == 1 and out[0].action == "imported"
    assert out[0].result.job_id == "job-1" and tr.calls == 1


def test_reconcile_saute_le_deja_connu_localement():
    tr = _FakeServerTransport()
    rec = _reconciler(tr)
    key = "visio|acct|occ-1|occ-1-rec"
    out = asyncio.run(rec.reconcile(OCC, already_imported={key}))
    assert out[0].action == "skipped_known" and tr.calls == 0  # pas de re-téléchargement


def test_rejeu_sans_memoire_locale_ne_double_pas():
    # « Crash » du connecteur → mémoire locale perdue → il ré-ingère, MAIS le serveur
    # déduplique sur l'Idempotency-Key : même job, aucun doublon (réconciliation sûre).
    tr = _FakeServerTransport()
    rec = _reconciler(tr)
    o1 = asyncio.run(rec.reconcile(OCC, already_imported=set()))
    o2 = asyncio.run(rec.reconcile(OCC, already_imported=set()))  # set vide = mémoire perdue
    assert o1[0].result.job_id == o2[0].result.job_id == "job-1"
    assert o2[0].result.idempotent is True


def test_service_run_once_et_start_stop():
    tr = _FakeServerTransport()
    svc = ConnectorService(_reconciler(tr), lambda: _discover(), interval_s=0.05)
    out = asyncio.run(svc.run_once())
    assert len(out) == 1 and out[0].action == "imported"
    asyncio.run(svc.start())
    assert svc.running is True
    asyncio.run(svc.stop())
    assert svc.running is False


async def _discover():
    return [OCC]


def test_service_run_forever_sarrete_sur_stop():
    async def scenario():
        tr = _FakeServerTransport()
        svc = ConnectorService(_reconciler(tr), _discover, interval_s=0.05)
        task = asyncio.create_task(svc.run_forever())
        await asyncio.sleep(0.12)          # laisse tourner quelques cycles
        await svc.stop()
        await asyncio.wait_for(task, timeout=2)
        return svc.running
    assert asyncio.run(scenario()) is False

"""A1 lot 3b — récepteur Flask du connecteur Visio (handler injecté, sans réseau)."""
from __future__ import annotations

import json
from pathlib import Path

from connector_service.app import create_connector_app
from connector_service.bridge import IngestResult
from connector_service.providers.visio import VisioTaskError

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "visio_task.json").read_text())


class _FakeHandler:
    def __init__(self, boom: bool = False):
        self.boom = boom
        self.seen: list = []

    async def handle(self, payload):
        self.seen.append(payload)
        if self.boom:
            raise RuntimeError("fetch/ingest cassé")
        if not payload.get("sub"):
            raise VisioTaskError("champ requis manquant: sub")
        return IngestResult(202, "job-x", False)


def _client(handler=None):
    app = create_connector_app(api_token="s3cr3t", handler=handler or _FakeHandler())
    return app.test_client()


def _auth():
    return {"Authorization": "Bearer s3cr3t"}


def test_health_ok():
    assert _client().get("/health").status_code == 200


def test_tache_valide_202_cree_job():
    r = _client().post("/api/v1/tasks/", json=FIXTURE, headers=_auth())
    assert r.status_code == 202
    body = r.get_json()
    assert body["job_id"] == "job-x" and body["idempotent"] is False


def test_sans_token_401():
    r = _client().post("/api/v1/tasks/", json=FIXTURE)
    assert r.status_code == 401


def test_mauvais_token_401():
    r = _client().post("/api/v1/tasks/", json=FIXTURE,
                       headers={"Authorization": "Bearer faux"})
    assert r.status_code == 401


def test_tache_invalide_400():
    bad = dict(FIXTURE)
    del bad["sub"]
    r = _client().post("/api/v1/tasks/", json=bad, headers=_auth())
    assert r.status_code == 400 and "sub" in r.get_json()["error"]


def test_ingestion_echec_502():
    r = _client(_FakeHandler(boom=True)).post("/api/v1/tasks/", json=FIXTURE, headers=_auth())
    assert r.status_code == 502

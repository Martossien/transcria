"""A1-A3 — app connecteur multi-plateforme (config-driven, handlers injectés)."""
from __future__ import annotations

import json
from pathlib import Path

from connector_service.app import build_handlers, create_connector_app
from connector_service.bridge import IngestResult
from connector_service.providers.visio import VisioTaskError

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "visio_task.json").read_text())


class _FakeHandler:
    def __init__(self, boom: Exception | None = None):
        self.boom = boom

    async def handle(self, payload):
        if self.boom:
            raise self.boom
        if not payload.get("sub"):
            raise VisioTaskError("champ requis manquant: sub")
        return IngestResult(202, "job-x", False)


def _app(config, handlers):
    return create_connector_app(config, handlers=handlers).test_client()


def test_health_liste_les_plateformes():
    c = _app({"visio": {"api_token": "s"}, "zoom": {"secret_token": "z"}},
             {"visio": _FakeHandler(), "zoom": _FakeHandler()})
    body = c.get("/health").get_json()
    assert body["status"] == "ok" and set(body["platforms"]) == {"visio", "zoom"}


def test_visio_monte_quand_present():
    c = _app({"visio": {"api_token": "s3cr3t"}}, {"visio": _FakeHandler()})
    r = c.post("/api/v1/tasks/", json=FIXTURE, headers={"Authorization": "Bearer s3cr3t"})
    assert r.status_code == 202 and r.get_json()["job_id"] == "job-x"


def test_visio_mauvais_token_401():
    c = _app({"visio": {"api_token": "s3cr3t"}}, {"visio": _FakeHandler()})
    r = c.post("/api/v1/tasks/", json=FIXTURE, headers={"Authorization": "Bearer faux"})
    assert r.status_code == 401


def test_plateforme_non_activee_pas_de_route():
    # Aucun handler → aucune route de réception (opt-in strict).
    c = create_connector_app({}, handlers={}).test_client()
    assert c.post("/api/v1/tasks/", json=FIXTURE).status_code == 404
    assert c.post("/webhooks/zoom", json={}).status_code == 404


def test_build_handlers_respecte_enabled():
    # Zoom activé seul → un handler zoom, pas visio/teams.
    handlers = build_handlers({
        "transcria_base_url": "http://127.0.0.1:7870", "transcria_api_token": "tia_x",
        "zoom": {"enabled": True, "secret_token": "z"},
        "visio": {"enabled": False}, "teams": {"enabled": False},
    })
    assert set(handlers) == {"zoom"}

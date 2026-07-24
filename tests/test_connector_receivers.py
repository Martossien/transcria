"""A2/A3 — récepteurs webhook Zoom + Teams (handler injecté, signature réelle calculée)."""
from __future__ import annotations

import json
from pathlib import Path

from flask import Flask

from connector_service.bridge import IngestResult
from connector_service.providers.zoom import ZoomEventError
from connector_service.receivers import register_teams_receiver, register_zoom_receiver
from connector_service.signatures import zoom_signature

FIX = Path(__file__).parent / "fixtures"
SECRET = "zoom-webhook-secret"


class _FakeHandler:
    def __init__(self, boom: Exception | None = None):
        self.boom = boom
        self.seen: list = []

    async def handle(self, payload):
        self.seen.append(payload)
        if self.boom:
            raise self.boom
        return IngestResult(202, "job-x", False)


# --------------------------------------------------------------------------- #
#  Zoom
# --------------------------------------------------------------------------- #
def _zoom_client(handler=None):
    app = Flask(__name__)
    register_zoom_receiver(app, secret_token=SECRET, handler=handler or _FakeHandler())
    return app.test_client()


def _post_zoom_signed(client, body: str):
    ts = "1784918039"
    sig = zoom_signature(SECRET, ts, body)
    return client.post("/webhooks/zoom", data=body, content_type="application/json",
                       headers={"x-zm-request-timestamp": ts, "x-zm-signature": sig})


def test_zoom_url_validation():
    r = _zoom_client().post("/webhooks/zoom", json={
        "event": "endpoint.url_validation", "payload": {"plainToken": "abc123"}})
    assert r.status_code == 200
    body = r.get_json()
    assert body["plainToken"] == "abc123" and len(body["encryptedToken"]) == 64


def test_zoom_signature_valide_202():
    body = (FIX / "zoom_recording_completed.json").read_text()
    r = _post_zoom_signed(_zoom_client(), body)
    assert r.status_code == 202 and r.get_json()["job_id"] == "job-x"


def test_zoom_signature_invalide_401():
    body = (FIX / "zoom_recording_completed.json").read_text()
    r = _zoom_client().post("/webhooks/zoom", data=body, content_type="application/json",
                            headers={"x-zm-request-timestamp": "1", "x-zm-signature": "v0=faux"})
    assert r.status_code == 401


def test_zoom_evenement_invalide_400():
    # Signé mais sans piste audio exploitable → le handler lève ZoomEventError → 400.
    body = json.dumps({"event": "recording.completed",
                       "payload": {"object": {"uuid": "u", "recording_files": []}}})
    r = _post_zoom_signed(_zoom_client(_FakeHandler(boom=ZoomEventError("aucune piste audio"))), body)
    assert r.status_code == 400 and "audio" in r.get_json()["error"]


# --------------------------------------------------------------------------- #
#  Teams
# --------------------------------------------------------------------------- #
def _teams_client(handler=None, client_state="cs-123"):
    app = Flask(__name__)
    register_teams_receiver(app, client_state=client_state, handler=handler or _FakeHandler())
    return app.test_client()


def test_teams_validation_abonnement_echo():
    r = _teams_client().post("/webhooks/teams?validationToken=VALIDATE-ME")
    assert r.status_code == 200 and r.get_data(as_text=True) == "VALIDATE-ME"


def test_teams_notification_valide_202():
    payload = json.loads((FIX / "teams_recording_notification.json").read_text())
    payload["value"][0]["clientState"] = "cs-123"
    r = _teams_client().post("/webhooks/teams", json=payload)
    assert r.status_code == 202


def test_teams_client_state_invalide_401():
    payload = json.loads((FIX / "teams_recording_notification.json").read_text())
    payload["value"][0]["clientState"] = "MAUVAIS"
    r = _teams_client().post("/webhooks/teams", json=payload)
    assert r.status_code == 401

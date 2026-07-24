"""A1 lot 3b — RequestsTransport, logique testée avec une session MOCKÉE (CI, sans réseau)."""
from __future__ import annotations

import asyncio

from connector_service.transports import RequestsTransport


class _FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class _FakeSession:
    def __init__(self, resp):
        self._resp = resp
        self.calls: list = []

    def request(self, method, url, headers, data, files, timeout):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "data": data, "files": files, "timeout": timeout})
        return self._resp


def test_transport_relaie_status_et_json():
    sess = _FakeSession(_FakeResp(202, {"job_id": "j1"}))
    tr = RequestsTransport(session=sess)
    status, body = asyncio.run(tr.request(
        "POST", "http://x/v1/audio/ingest",
        headers={"Authorization": "Bearer t"}, data={"provider": "visio"},
        files={"file": ("f.wav", b"AUD")}))
    assert status == 202 and body == {"job_id": "j1"}
    call = sess.calls[0]
    assert call["method"] == "POST" and call["files"]["file"] == ("f.wav", b"AUD")


def test_transport_reponse_non_json_corps_vide():
    class _Bad(_FakeResp):
        def json(self):
            raise ValueError("pas du JSON")
    tr = RequestsTransport(session=_FakeSession(_Bad(500, None)))
    status, body = asyncio.run(tr.request("GET", "http://x", headers={}))
    assert status == 500 and body == {}

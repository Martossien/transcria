"""Intégration réseau de l'appel direct au fournisseur LLM « cloud » — vrai socket, sans GPU.

Un faux fournisseur OpenAI-compatible (http.server en thread) exige un Bearer ; on valide
le chemin complet ``chat_completion`` / ``CloudLLMBackend.list_models`` sur le fil.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from builders import make_config
from net_helpers import free_port

from transcria.gpu.llm_backend import CloudLLMBackend, create_llm_backend
from transcria.workflow.refine_llm import CloudLLMError, chat_completion

pytestmark = pytest.mark.integration

GOOD_KEY = "cle-attendue"


class _Handler(BaseHTTPRequestHandler):
    hits: list = []
    forced_status: int | None = None
    forced_body: dict | None = None

    def log_message(self, *args):  # silence
        pass

    def _send(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _auth_ok(self) -> bool:
        if self.headers.get("Authorization") != f"Bearer {GOOD_KEY}":
            self._send(401, {"error": {"message": "invalid api key"}})
            return False
        return True

    def do_GET(self):
        if self.path == "/v1/models" and self._auth_ok():
            self._send(200, {"data": [{"id": "modele-test"}, {"id": "autre"}]})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).hits.append(body)
        if not self._auth_ok():
            return
        if self.forced_status:
            self._send(self.forced_status, self.forced_body or {"error": {"message": "forcé"}})
            return
        if body.get("model") != "modele-test":
            self._send(404, {"error": {"message": "model 'inconnu' not found"}})
            return
        self._send(200, {"choices": [{"message": {"content": "<think>t</think>Bonjour cloud."}}]})


@pytest.fixture
def provider():
    handler = type("H", (_Handler,), {"hits": [], "forced_status": None, "forced_body": None})
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield handler, port
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def _config(port: int, model: str = "modele-test"):
    return make_config({
        "services": {"backend": "cloud"},
        "workflow": {
            "arbitration_llm": {"enabled": True, "model_id": f"fakeprov/{model}",
                                "api_base": f"http://127.0.0.1:{port}/v1"},
            "llm_cloud": {"api_key_env": "FAKEPROV_API_KEY"},
        },
    })


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("FAKEPROV_API_KEY", GOOD_KEY)


MSG = [{"role": "user", "content": "?"}]


def test_nominal_filtre_think(provider):
    handler, port = provider
    assert chat_completion(_config(port), MSG, timeout_s=10) == "Bonjour cloud."
    assert handler.hits[0]["model"] == "modele-test"
    assert "chat_template_kwargs" not in handler.hits[0]


def test_cle_fausse_401_sans_retry(provider, monkeypatch):
    handler, port = provider
    monkeypatch.setenv("FAKEPROV_API_KEY", "mauvaise")
    with pytest.raises(CloudLLMError) as exc:
        chat_completion(_config(port), MSG, timeout_s=10)
    assert "401" in str(exc.value) and "invalid api key" in str(exc.value)
    assert "mauvaise" not in str(exc.value)
    assert len(handler.hits) == 1


@pytest.mark.parametrize("status", [429, 503])
def test_statuts_fournisseur(provider, status):
    handler, port = provider
    handler.forced_status = status
    handler.forced_body = {"error": {"message": "surcharge"}}
    with pytest.raises(CloudLLMError) as exc:
        chat_completion(_config(port), MSG, timeout_s=10)
    assert str(status) in str(exc.value) and "surcharge" in str(exc.value)
    assert len(handler.hits) == 1


def test_modele_inconnu(provider):
    _, port = provider
    with pytest.raises(CloudLLMError) as exc:
        chat_completion(_config(port, "inconnu"), MSG, timeout_s=10)
    assert "404" in str(exc.value) and "not found" in str(exc.value)


def test_list_models(provider, monkeypatch):
    _, port = provider
    backend = create_llm_backend(_config(port))
    assert isinstance(backend, CloudLLMBackend)
    assert backend.list_models(timeout=10) == (["modele-test", "autre"], None)
    monkeypatch.setenv("FAKEPROV_API_KEY", "mauvaise")
    ids, err = backend.list_models(timeout=10)
    assert ids is None and err and "401" in err

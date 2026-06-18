from __future__ import annotations

import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "smoke_resource_node.py"
_SPEC = importlib.util.spec_from_file_location("smoke_resource_node", _SCRIPT)
smoke = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules[_SPEC.name] = smoke
_SPEC.loader.exec_module(smoke)


class _ResourceNodeHandler(BaseHTTPRequestHandler):
    require_auth = True
    health_status = 200

    def log_message(self, _format, *args):
        return

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send_json(self.health_status, {"status": "ok" if self.health_status == 200 else "down"})
            return
        if self.path == "/capabilities":
            self._send_json(200, {"service": "transcria-inference", "deployment_mode": "resource_node"})
            return
        self._send_json(404, {"error": "not_found"})

    def do_POST(self):
        if self.path != "/engines/ensure":
            self._send_json(404, {"error": "not_found"})
            return
        if self.require_auth and self.headers.get("Authorization") != "Bearer secret":
            self._send_json(401, {"error": "unauthorized"})
            return
        self._send_json(404, {"error": "unknown_engine", "available": []})


@pytest.fixture
def resource_node_server():
    _ResourceNodeHandler.require_auth = True
    _ResourceNodeHandler.health_status = 200
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ResourceNodeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_smoke_resource_node_with_api_key(resource_node_server):
    messages = smoke.run_smoke(resource_node_server, api_key="secret", timeout_s=1)

    assert messages == [
        "[OK] /health",
        "[OK] /capabilities",
        "[OK] /engines/ensure refuse les requêtes sans clé",
        "[OK] /engines/ensure accepte la clé API (moteur factice non lancé)",
    ]


def test_smoke_resource_node_without_api_key_skips_auth_probe(resource_node_server):
    messages = smoke.run_smoke(resource_node_server, timeout_s=1)

    assert messages[-1] == "[WARN] clé API absente : probe d'auth /engines/ensure ignoré"


def test_smoke_resource_node_fails_if_auth_is_not_enforced(resource_node_server):
    _ResourceNodeHandler.require_auth = False

    with pytest.raises(RuntimeError, match="sans clé attendu 401"):
        smoke.run_smoke(resource_node_server, api_key="secret", timeout_s=1)


def test_smoke_resource_node_main_reads_api_key_from_env(resource_node_server, monkeypatch, capsys):
    monkeypatch.setenv("TRANSCRIA_INFERENCE_API_KEY", "secret")

    assert smoke.main(["--url", resource_node_server, "--timeout", "1"]) == 0

    out = capsys.readouterr().out
    assert "[OK] smoke resource-node terminé" in out

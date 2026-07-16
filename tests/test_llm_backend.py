

class TestHttpGetJsonResult:
    """C2.4 — la nature des échecs réseau est distinguée (plus de None silencieux)."""

    def _result(self, url, timeout=1):
        from transcria.gpu.llm_backend import LLMBackend
        return LLMBackend._http_get_json_result(url, timeout=timeout)

    def test_connexion_refusee(self):
        data, err = self._result("http://127.0.0.1:19999/api/tags")
        assert data is None and err is not None
        assert "connexion" in err or "refusée" in err

    def test_dns_impossible(self):
        data, err = self._result("http://hote-inexistant-transcria.invalid/api")
        assert data is None and err is not None
        assert "DNS" in err or "connexion" in err

    def test_statut_http_distingue(self, tmp_path):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(503)
                self.end_headers()
            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.handle_request, daemon=True).start()
        data, err = self._result(f"http://127.0.0.1:{srv.server_port}/x")
        srv.server_close()
        assert data is None and err == "statut HTTP 503"

    def test_json_invalide_distingue(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"pas du json{")
            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.handle_request, daemon=True).start()
        data, err = self._result(f"http://127.0.0.1:{srv.server_port}/x")
        srv.server_close()
        assert data is None and err is not None and "non-JSON" in err

    def test_succes(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"models": []}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.handle_request, daemon=True).start()
        data, err = self._result(f"http://127.0.0.1:{srv.server_port}/x")
        srv.server_close()
        assert err is None and data == {"models": []}

    def test_throttle_des_logs(self, caplog):
        import logging

        from transcria.gpu.llm_backend import LLMBackend
        LLMBackend._NETWORK_ERROR_LOGGED.clear()
        with caplog.at_level(logging.WARNING):
            LLMBackend._http_get_json("http://127.0.0.1:19999/api/ps", timeout=1)
            LLMBackend._http_get_json("http://127.0.0.1:19999/api/ps", timeout=1)
        warnings = [r for r in caplog.records if "injoignable" in r.getMessage()]
        assert len(warnings) == 1          # 2 échecs, 1 seul log (throttle 5 min)

#!/usr/bin/env python3
"""Smoke test léger du service `inference_service` resource-node.

Ce script vérifie le plan de contrôle sans charger de modèle GPU :

- `/health` répond 200 ;
- `/capabilities` répond 200 et expose le service attendu ;
- si une clé API est fournie, `/engines/ensure` refuse une requête sans clé puis
  accepte la clé sur un moteur factice (réponse 404 `unknown_engine`, sans lancement).

Exemples :
    venv/bin/python scripts/smoke_resource_node.py
    TRANSCRIA_INFERENCE_API_KEY=... venv/bin/python scripts/smoke_resource_node.py --api-key-env TRANSCRIA_INFERENCE_API_KEY
    venv/bin/python scripts/smoke_resource_node.py --url http://gpu-node:8002 --api-key "$TRANSCRIA_INFERENCE_API_KEY"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: dict[str, Any]


def _json_request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    api_key: str | None = None,
    timeout_s: float = 5.0,
) -> HttpResponse:
    url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = Request(url, data=data, headers=headers, method=method)

    try:
        with urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8")
            return HttpResponse(resp.status, json.loads(raw) if raw else {})
    except HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {"raw": raw}
        return HttpResponse(exc.code, body)
    except URLError as exc:
        raise RuntimeError(f"{url} injoignable: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError(f"{url} timeout après {timeout_s:.1f}s") from exc


def run_smoke(base_url: str, *, api_key: str | None = None, timeout_s: float = 5.0) -> list[str]:
    messages: list[str] = []

    health = _json_request(base_url, "/health", timeout_s=timeout_s)
    if health.status != 200:
        raise RuntimeError(f"/health attendu 200, reçu {health.status}: {health.body}")
    messages.append("[OK] /health")

    capabilities = _json_request(base_url, "/capabilities", timeout_s=timeout_s)
    if capabilities.status != 200:
        raise RuntimeError(f"/capabilities attendu 200, reçu {capabilities.status}: {capabilities.body}")
    if capabilities.body.get("service") != "transcria-inference":
        raise RuntimeError("/capabilities ne ressemble pas à un service TranscrIA inference_service")
    messages.append("[OK] /capabilities")

    if not api_key:
        messages.append("[WARN] clé API absente : probe d'auth /engines/ensure ignoré")
        return messages

    probe_payload = {"engine": "__smoke_auth_probe__"}
    unauth = _json_request(base_url, "/engines/ensure", method="POST", payload=probe_payload, timeout_s=timeout_s)
    if unauth.status != 401:
        raise RuntimeError(f"/engines/ensure sans clé attendu 401, reçu {unauth.status}: {unauth.body}")
    messages.append("[OK] /engines/ensure refuse les requêtes sans clé")

    auth = _json_request(base_url, "/engines/ensure", method="POST", payload=probe_payload, api_key=api_key, timeout_s=timeout_s)
    if auth.status == 401:
        raise RuntimeError("/engines/ensure refuse la clé API fournie")
    if auth.status != 404 or auth.body.get("error") != "unknown_engine":
        raise RuntimeError(f"/engines/ensure avec moteur factice attendu 404 unknown_engine, reçu {auth.status}: {auth.body}")
    messages.append("[OK] /engines/ensure accepte la clé API (moteur factice non lancé)")
    return messages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:8002", help="URL du service inference_service")
    parser.add_argument("--api-key", default=None, help="clé API attendue par /infer/* et /engines/*")
    parser.add_argument("--api-key-env", default="TRANSCRIA_INFERENCE_API_KEY", help="variable d'environnement portant la clé API")
    parser.add_argument("--timeout", type=float, default=5.0, help="timeout HTTP par requête")
    args = parser.parse_args(argv)

    api_key = args.api_key or os.environ.get(args.api_key_env)
    try:
        for line in run_smoke(args.url, api_key=api_key, timeout_s=args.timeout):
            print(line)
    except RuntimeError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    print("[OK] smoke resource-node terminé")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

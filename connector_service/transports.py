"""Transports HTTP réels pour le pont (A1 — implémentations de `Transport`).

`RequestsTransport` enveloppe `requests` (déjà dans TranscrIA) ; l'appel BLOQUANT tourne
dans un exécuteur pour ne pas figer l'event loop async. La session est INJECTABLE : la CI
teste la logique avec une session mockée, sans réseau.
"""
from __future__ import annotations

import asyncio


class RequestsTransport:
    def __init__(self, session=None, timeout: float = 30.0) -> None:
        self._session = session      # injectée (tests) ; sinon module `requests` paresseux
        self._timeout = timeout

    def _do(self, method, url, headers, data, files) -> tuple[int, dict]:
        sess = self._session
        if sess is None:
            import requests  # déjà une dépendance TranscrIA

            sess = requests
        resp = sess.request(method, url, headers=headers, data=data, files=files,
                            timeout=self._timeout)
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001 — réponse non-JSON → corps vide, on garde le code
            body = {}
        return resp.status_code, (body if isinstance(body, dict) else {})

    async def request(self, method, url, *, headers, data=None, files=None) -> tuple[int, dict]:
        return await asyncio.get_event_loop().run_in_executor(
            None, self._do, method, url, headers, data, files)

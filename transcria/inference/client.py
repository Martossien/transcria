"""Client HTTP du service d'inférence distant.

Réutilisable par tous les adaptateurs `Remote*` du frontend. Gère :
  - l'authentification (clé API en Bearer, lue d'une variable d'env) ;
  - les deux transports : référence fichier (mono-machine) et upload (distant) ;
  - les timeouts et un retry léger sur erreurs transitoires ;
  - une distinction nette entre **indisponibilité** (réseau/5xx/503 → fallback
    possible) et **erreur de requête** (4xx métier → définitive).

Distinction d'erreurs (cruciale pour le fallback) :
  - `InferenceUnavailable`  : timeout, connexion, 5xx, 503 gpu_busy → le caller
                              peut basculer en local (`fallback_local`).
  - `InferenceRequestError` : 400/401/403/422 → l'entrée est en cause, pas de
                              fallback (rejouer en local échouerait pareil, sauf 401).
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 1800
_DEFAULT_RETRIES = 2
_RETRY_BACKOFF_S = 1.5
_UNAVAILABLE_STATUSES = {502, 503, 504}


class InferenceClientError(RuntimeError):
    """Base des erreurs du client d'inférence."""


class InferenceUnavailable(InferenceClientError):
    """Service injoignable ou temporairement indisponible (réseau, 5xx, 503).

    Déclenche le fallback local si `fallback_local` est activé.
    """


class InferenceRequestError(InferenceClientError):
    """Erreur de requête métier (4xx) renvoyée par le service.

    Porte le code/message du service. Pas de fallback (l'entrée est en cause).
    """

    def __init__(self, message: str, *, status: int, code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class InferenceClient:
    """Client typé pour `inference_service`.

    Args:
        base_url: racine du service, ex. ``http://127.0.0.1:8002``.
        api_key: clé API (Bearer). None → pas d'en-tête d'auth.
        transport: ``"file_ref"`` (envoie un chemin) ou ``"upload"`` (multipart).
        timeout_s: délai par requête.
        retries: tentatives supplémentaires sur erreur transitoire.
        session: session HTTP injectable (tests). Sinon `requests`.
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        transport: str = "file_ref",
        timeout_s: int = _DEFAULT_TIMEOUT_S,
        retries: int = _DEFAULT_RETRIES,
        session: object | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.transport = transport if transport in ("file_ref", "upload") else "file_ref"
        self.timeout_s = int(timeout_s)
        self.retries = max(0, int(retries))
        # Duck-typé : `requests` (module) ou toute session exposant get/post (tests).
        self._session: Any = session or requests

    # ── Endpoints ─────────────────────────────────────────────────────────────

    def health(self) -> bool:
        """True si le service répond 200 sur /health (sonde rapide, sans auth)."""
        try:
            resp = self._session.get(f"{self.base_url}/health", timeout=5)
            return resp.status_code == 200
        except Exception as exc:  # noqa: BLE001
            logger.debug("Inference /health injoignable: %s", exc)
            return False

    def capabilities(self) -> dict:
        """Inventaire du nœud via /capabilities (mode, GPU, moteurs, santé).

        Lève `InferenceUnavailable` si le service est injoignable (réseau/5xx) —
        signal de mode dégradé pour la frontale.
        """
        url = f"{self.base_url}/capabilities"
        try:
            resp = self._session.get(url, headers=self._headers(), timeout=5)
        except requests.exceptions.RequestException as exc:
            raise InferenceUnavailable(f"{url} injoignable: {exc}") from exc
        return self._parse(resp, url)

    def diarize(self, audio_path: Path) -> dict:
        """Diarise un audio via /infer/diarize. Retourne le dict canonique."""
        return self._post_audio("/infer/diarize", audio_path)

    def voice_embed(self, audio_path: Path) -> dict:
        """Empreinte vocale via /infer/voice-embed. Retourne le payload embedding."""
        return self._post_audio("/infer/voice-embed", audio_path)

    # ── Transport / retry ──────────────────────────────────────────────────────

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def _post_audio(self, path: str, audio_path: Path) -> dict:
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = self._send(url, audio_path)
                return self._parse(resp, url)
            except InferenceUnavailable as exc:
                last_exc = exc
                if attempt < self.retries:
                    delay = _RETRY_BACKOFF_S * (attempt + 1)
                    logger.warning("Inference %s indispo (tentative %d/%d) — retry dans %.1fs : %s",
                                   url, attempt + 1, self.retries + 1, delay, exc)
                    time.sleep(delay)
                    continue
                raise
        raise last_exc or InferenceUnavailable(f"{url} indisponible")

    def _send(self, url: str, audio_path: Path):
        try:
            if self.transport == "upload":
                with open(audio_path, "rb") as fh:
                    files = {"file": (audio_path.name, fh)}
                    return self._session.post(url, files=files, headers=self._headers(), timeout=self.timeout_s)
            return self._session.post(
                url, json={"audio_path": str(audio_path)}, headers=self._headers(), timeout=self.timeout_s
            )
        except requests.exceptions.RequestException as exc:
            # Timeout, connexion refusée, DNS… = indisponibilité → fallback possible.
            raise InferenceUnavailable(f"{url} injoignable: {exc}") from exc

    @staticmethod
    def _parse(resp, url: str) -> dict:
        status = resp.status_code
        if status == 200:
            return resp.json()
        # Corps d'erreur du service : {"error": code, "message": ...}
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = {}
        code = str(body.get("error", ""))
        message = str(body.get("message", body or resp.text))
        if status in _UNAVAILABLE_STATUSES:
            raise InferenceUnavailable(f"{url} → {status} {code}: {message}")
        raise InferenceRequestError(f"{url} → {status} {code}: {message}", status=status, code=code)


def build_client_from_config(config: dict) -> InferenceClient | None:
    """Construit un client depuis la section `inference` de la config.

    Retourne None si aucune URL n'est configurée (mode local).
    """
    inf = config.get("inference", {}) or {}
    url = inf.get("url") or inf.get("base_url")
    if not url:
        return None
    auth = inf.get("auth", {}) or {}
    api_key = None
    if auth.get("api_key_env"):
        api_key = os.environ.get(auth["api_key_env"])
    api_key = api_key or auth.get("api_key")
    transport = (inf.get("transport", {}) or {}).get("audio", "file_ref")
    transport = "upload" if transport == "upload" else "file_ref"
    return InferenceClient(
        url,
        api_key=api_key,
        transport=transport,
        timeout_s=int((inf.get("resilience", {}) or {}).get("timeout_s", _DEFAULT_TIMEOUT_S)),
        retries=int((inf.get("resilience", {}) or {}).get("retries", _DEFAULT_RETRIES)),
    )

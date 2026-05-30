"""Client HTTP pour un serveur ASR compatible OpenAI (vLLM, SGLang, …).

Distinct de `InferenceClient` (service Flask maison : diarisation, empreinte
vocale). Ici on parle à l'endpoint OpenAI standard exposé par le moteur de
serving — quel qu'il soit — pour les modèles STT :

    POST {base_url}/audio/transcriptions      (multipart : file, model, language…)

où `base_url` se termine généralement par ``/v1`` (ex. ``http://HOST:8003/v1``).
Aucune dépendance à un moteur précis : seul compte le protocole OpenAI.

Transport : toujours un upload multipart du WAV. Le serveur ne lit pas de chemin
local (il peut être sur une autre machine), donc pas de mode « file_ref » ici.

Distinction d'erreurs (réutilisée du client maison, pour un fallback homogène) :
  - `InferenceUnavailable`  : timeout, connexion, 5xx, 503 → fallback local possible.
  - `InferenceRequestError` : 4xx (400 format audio, 401 clé, 404 modèle) → définitif.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import requests

from transcria.inference.client import (
    InferenceRequestError,
    InferenceUnavailable,
)

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 600
_DEFAULT_RETRIES = 2
_RETRY_BACKOFF_S = 1.5
_UNAVAILABLE_STATUSES = {502, 503, 504}


class AsrClient:
    """Client typé pour l'endpoint ASR OpenAI d'un serveur vLLM.

    Args:
        base_url: racine de l'API, ex. ``http://127.0.0.1:8001/v1``.
        model: nom du modèle servi (``--served-model-name`` côté vLLM).
        api_key: clé API (Bearer) si vLLM est lancé avec ``--api-key``. None sinon.
        response_format: ``verbose_json`` (segments + timestamps) ou ``json`` (texte seul).
        timeout_s: délai par requête.
        retries: tentatives supplémentaires sur erreur transitoire.
        session: session HTTP injectable (tests). Sinon le module `requests`.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str | None = None,
        response_format: str = "verbose_json",
        timeout_s: int = _DEFAULT_TIMEOUT_S,
        retries: int = _DEFAULT_RETRIES,
        session: object | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key or None
        self.response_format = response_format if response_format in ("verbose_json", "json", "text") else "verbose_json"
        self.timeout_s = int(timeout_s)
        self.retries = max(0, int(retries))
        self._session: Any = session or requests

    # ── Endpoints ───────────────────────────────────────────────────────────--

    def health(self) -> bool:
        """True si le serveur répond et sert bien le modèle attendu (/models)."""
        try:
            resp = self._session.get(f"{self.base_url}/models", headers=self._headers(), timeout=5)
        except Exception as exc:  # noqa: BLE001
            logger.debug("ASR /models injoignable (%s): %s", self.base_url, exc)
            return False
        if resp.status_code != 200:
            return False
        try:
            ids = {m.get("id") for m in (resp.json() or {}).get("data", [])}
        except Exception:  # noqa: BLE001
            return True  # serveur up mais corps inattendu : on ne bloque pas
        if self.model and ids and self.model not in ids:
            logger.warning("ASR %s : modèle '%s' absent de /models %s", self.base_url, self.model, sorted(ids))
        return True

    def transcribe(self, wav_path: Path, *, language: str = "fr", prompt: str | None = None) -> dict:
        """Transcrit un WAV via /audio/transcriptions. Retourne le JSON OpenAI brut.

        Le fichier DOIT être un WAV (ou OGG) : l'endpoint rejette le MP3 en upload
        (bug observé sur vLLM). La conversion est de la responsabilité de l'appelant.
        """
        url = f"{self.base_url}/audio/transcriptions"
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = self._send(url, wav_path, language=language, prompt=prompt)
                return self._parse(resp, url)
            except InferenceUnavailable as exc:
                last_exc = exc
                if attempt < self.retries:
                    delay = _RETRY_BACKOFF_S * (attempt + 1)
                    logger.warning(
                        "ASR %s indispo (tentative %d/%d) — retry dans %.1fs : %s",
                        url, attempt + 1, self.retries + 1, delay, exc,
                    )
                    time.sleep(delay)
                    continue
                raise
        raise last_exc or InferenceUnavailable(f"{url} indisponible")

    # ── Transport / parsing ─────────────────────────────────────────────────--

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def _send(self, url: str, wav_path: Path, *, language: str, prompt: str | None):
        data = {"model": self.model, "response_format": self.response_format}
        if language:
            data["language"] = language
        if prompt:
            data["prompt"] = prompt
        try:
            with open(wav_path, "rb") as fh:
                files = {"file": (Path(wav_path).name, fh, "audio/wav")}
                return self._session.post(
                    url, data=data, files=files, headers=self._headers(), timeout=self.timeout_s
                )
        except FileNotFoundError:
            raise
        except requests.exceptions.RequestException as exc:
            # Timeout, connexion refusée, DNS… = indisponibilité → fallback possible.
            raise InferenceUnavailable(f"{url} injoignable: {exc}") from exc

    @staticmethod
    def _parse(resp, url: str) -> dict:
        status = resp.status_code
        if status == 200:
            try:
                return resp.json()
            except Exception as exc:  # noqa: BLE001 — réponse 200 mais corps illisible
                raise InferenceRequestError(
                    f"{url} → 200 mais JSON invalide: {exc}", status=200, code="bad_response"
                ) from exc
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = {}
        # OpenAI : {"error": {"message", "type", "code"}}. vLLM peut varier.
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            message = str(err.get("message", err))
            code = str(err.get("code") or err.get("type") or "")
        else:
            message = str(body or getattr(resp, "text", ""))
            code = ""
        if status in _UNAVAILABLE_STATUSES:
            raise InferenceUnavailable(f"{url} → {status} {code}: {message}")
        raise InferenceRequestError(f"{url} → {status} {code}: {message}", status=status, code=code)


def build_asr_client_from_config(config: dict, backend: str) -> AsrClient | None:
    """Construit un `AsrClient` pour `backend` depuis `inference.stt.backends`.

    Retourne None si aucune URL n'est configurée pour ce backend (→ mode local).
    """
    import os

    inf = config.get("inference", {}) or {}
    stt = inf.get("stt", {}) or {}
    backends = stt.get("backends", {}) or {}
    spec = backends.get(backend, {}) or {}
    url = spec.get("url")
    if not url:
        return None

    auth = stt.get("auth", {}) or {}
    api_key = None
    if auth.get("api_key_env"):
        api_key = os.environ.get(auth["api_key_env"])
    api_key = api_key or auth.get("api_key") or None

    # response_format par backend (certains moteurs ne supportent pas verbose_json :
    # p.ex. Cohere Transcribe sur vLLM → 400). Repli sur la valeur globale.
    response_format = spec.get("response_format") or stt.get("response_format", "verbose_json")

    return AsrClient(
        url,
        model=spec.get("model") or backend,
        api_key=api_key,
        response_format=response_format,
        timeout_s=int(stt.get("timeout_s", _DEFAULT_TIMEOUT_S)),
        retries=int(stt.get("retries", _DEFAULT_RETRIES)),
    )

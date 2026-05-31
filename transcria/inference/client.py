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
from collections.abc import Callable
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

    def ensure_engine(self, name: str) -> dict:
        """Demande au nœud d'assurer le moteur STT `name` (cycle de vie A/B/C).

        Retourne {engine, status, gpu_index, reason}. `status` ∈ ready/launched/busy.
        Lève `InferenceUnavailable` si le nœud est injoignable ou renvoie 503 (busy).
        """
        url = f"{self.base_url}/engines/ensure"
        try:
            resp = self._session.post(
                url, json={"engine": name}, headers=self._headers(), timeout=self.timeout_s
            )
        except requests.exceptions.RequestException as exc:
            raise InferenceUnavailable(f"{url} injoignable: {exc}") from exc
        return self._parse(resp, url)  # 503 busy → InferenceUnavailable ; 404 → RequestError

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


class FailoverInferenceClient(InferenceClient):
    """Client à bascule **actif/passif** sur une liste ordonnée de nœuds (C6 / B7).

    Vise le premier nœud joignable par ordre de priorité ; sur `InferenceUnavailable`
    (réseau/5xx/503), bascule **automatiquement** vers le suivant. Une
    `InferenceRequestError` (4xx métier) ne déclenche **pas** de bascule : l'entrée est
    en cause, rejouer sur un autre nœud échouerait pareil.

    La sélection est **recalculée à chaque appel** (jamais persistée) : quand le nœud
    principal revient, les appels suivants y repartent (préférence à la priorité) — pas
    de split-brain, aucun état partagé. La résilience par job (§7.2 :
    defer/requeue_later/max_unavailable_s) couvre la fenêtre de bascule.
    """

    def __init__(self, clients: list[InferenceClient]) -> None:
        if not clients:
            raise ValueError("FailoverInferenceClient exige au moins un nœud")
        primary = clients[0]
        super().__init__(
            primary.base_url,
            api_key=primary.api_key,
            transport=primary.transport,
            timeout_s=primary.timeout_s,
            retries=primary.retries,
            session=primary._session,
        )
        self._clients = list(clients)

    @property
    def nodes(self) -> list[str]:
        """URLs des nœuds dans l'ordre de priorité."""
        return [c.base_url for c in self._clients]

    def _failover(self, op: Callable[[InferenceClient], dict], label: str) -> dict:
        last_exc: InferenceUnavailable | None = None
        for idx, client in enumerate(self._clients):
            try:
                return op(client)
            except InferenceUnavailable as exc:
                last_exc = exc
                if idx + 1 < len(self._clients):
                    logger.warning(
                        "Nœud d'inférence %s indisponible (%s) — bascule vers %s",
                        client.base_url, label, self._clients[idx + 1].base_url,
                    )
                else:
                    logger.error(
                        "Tous les nœuds d'inférence sont indisponibles (%s) : %s", label, exc
                    )
        raise last_exc or InferenceUnavailable(f"aucun nœud joignable ({label})")

    def health(self) -> bool:
        """True si **au moins un** nœud répond (le service est servi-able)."""
        return any(c.health() for c in self._clients)

    def capabilities(self) -> dict:
        return self._failover(lambda c: c.capabilities(), "capabilities")

    def ensure_engine(self, name: str) -> dict:
        return self._failover(lambda c: c.ensure_engine(name), f"ensure:{name}")

    def diarize(self, audio_path: Path) -> dict:
        return self._failover(lambda c: c.diarize(audio_path), "diarize")

    def voice_embed(self, audio_path: Path) -> dict:
        return self._failover(lambda c: c.voice_embed(audio_path), "voice_embed")


def _resolve_node_urls(inf: dict) -> list[str]:
    """Liste ordonnée (priorité croissante) des URLs de nœuds depuis `inference`.

    Accepte `inference.nodes` (liste ordonnée de `{url, priority}` ou de chaînes) et
    retombe sur `inference.url`/`base_url` (un seul nœud) pour la compat ascendante.
    Les doublons et les entrées vides sont ignorés ; l'ordre de la config départage les
    priorités égales.
    """
    entries: list[tuple[int, int, str]] = []
    nodes = inf.get("nodes")
    if isinstance(nodes, list):
        for index, node in enumerate(nodes):
            if isinstance(node, dict):
                url = str(node.get("url") or node.get("base_url") or "").strip()
                priority = node.get("priority", index + 1)
            elif isinstance(node, str):
                url, priority = node.strip(), index + 1
            else:
                continue
            try:
                priority = int(priority)
            except (TypeError, ValueError):
                priority = index + 1
            if url:
                entries.append((priority, index, url))
    entries.sort(key=lambda e: (e[0], e[1]))
    urls = [url for _, _, url in entries]
    if not urls:
        single = str(inf.get("url") or inf.get("base_url") or "").strip()
        if single:
            urls = [single]
    seen: set[str] = set()
    deduped: list[str] = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped


def build_client_from_config(config: dict) -> InferenceClient | None:
    """Construit un client depuis la section `inference` de la config.

    Retourne None si aucun nœud n'est configuré (mode local). Avec **un** nœud, retourne
    un `InferenceClient` simple ; avec **plusieurs** (`inference.nodes`), un
    `FailoverInferenceClient` actif/passif (C6 / B7). L'auth, le transport et la
    résilience sont partagés par tous les nœuds.
    """
    inf = config.get("inference", {}) or {}
    urls = _resolve_node_urls(inf)
    if not urls:
        return None
    auth = inf.get("auth", {}) or {}
    api_key = None
    if auth.get("api_key_env"):
        api_key = os.environ.get(auth["api_key_env"])
    api_key = api_key or auth.get("api_key")
    transport = (inf.get("transport", {}) or {}).get("audio", "file_ref")
    transport = "upload" if transport == "upload" else "file_ref"
    resilience = inf.get("resilience", {}) or {}
    timeout_s = int(resilience.get("timeout_s", _DEFAULT_TIMEOUT_S))
    retries = int(resilience.get("retries", _DEFAULT_RETRIES))
    clients = [
        InferenceClient(url, api_key=api_key, transport=transport, timeout_s=timeout_s, retries=retries)
        for url in urls
    ]
    return clients[0] if len(clients) == 1 else FailoverInferenceClient(clients)

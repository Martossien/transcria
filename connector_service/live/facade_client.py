"""Client HTTP de la façade TranscrIA — `POST /v1/audio/transcriptions` (OpenAI-audio).

C'est la frontière voulue par l'architecture : le connecteur ne parle JAMAIS au cœur en
Python, il l'appelle en HTTP comme n'importe quel client (contrat d'isolation). Une fenêtre
audio d'un locuteur entre, du texte sort.
"""
from __future__ import annotations

from typing import Any


class FacadeError(RuntimeError):
    """La façade a refusé la fenêtre (jeton invalide, endpoint désactivé, plafond…)."""


def facade_transcriber(base_url: str, token: str, *, model: str = "whisper-1",
                       language: str | None = None, timeout_s: float = 120.0,
                       session: Any = None):
    """Retourne `transcribe(wav_bytes) -> texte` prêt pour `FacadeTranscriber`.

    `session` (injectable) permet de réutiliser une connexion et de tester sans réseau.
    """
    import requests  # dép TranscrIA

    http = session or requests.Session()
    url = f"{base_url.rstrip('/')}/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {token}"}

    def transcribe(wav: bytes) -> str:
        data = {"model": model, "response_format": "json"}
        if language:
            data["language"] = language
        resp = http.post(url, headers=headers, timeout=timeout_s,
                         files={"file": ("window.wav", wav, "audio/wav")}, data=data)
        if resp.status_code != 200:
            raise FacadeError(f"façade HTTP {resp.status_code}: {resp.text[:180]}")
        try:
            return str((resp.json() or {}).get("text") or "")
        except ValueError as exc:
            raise FacadeError("réponse de façade illisible (JSON attendu)") from exc

    return transcribe

"""Exceptions du service d'inférence et leur mapping HTTP.

Hiérarchie volontairement plate : chaque erreur porte son code HTTP, son code
métier (stable, pour le client) et un message. Les routes n'ont qu'à attraper
`InferenceError` et sérialiser.
"""
from __future__ import annotations


class InferenceError(Exception):
    """Base des erreurs métier du service. Porte un statut HTTP et un code stable."""

    http_status: int = 500
    code: str = "internal_error"
    retry_after: int | None = None

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        if code:
            self.code = code

    def to_dict(self) -> dict:
        payload: dict = {"error": self.code, "message": self.message}
        if self.retry_after is not None:
            payload["retry_after"] = self.retry_after
        return payload


class BadRequestError(InferenceError):
    """Entrée client invalide (champ manquant, fichier absent, format inattendu)."""

    http_status = 400
    code = "bad_request"


class UnprocessableError(InferenceError):
    """L'audio est valide en transport mais l'inférence métier a échoué (ex. embedding vide)."""

    http_status = 422
    code = "unprocessable"


class GpuBusyError(InferenceError):
    """CAS C : VRAM indisponible. Le client (file frontend) doit re-planifier.

    Renvoie 503 + Retry-After pour signaler un repli temporaire, pas un échec
    définitif. Voir docs/MIGRATION_API_SERVEUR_GPU.md (§4bis.3).
    """

    http_status = 503
    code = "gpu_busy"

    def __init__(self, message: str = "", *, retry_after: int = 30) -> None:
        super().__init__(message)
        self.retry_after = retry_after

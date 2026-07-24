"""Auto-diagnostic du service connecteur (A0 — DoD « doctor du service »).

Volontairement SANS réseau ni import de transcria : valide la configuration minimale
d'un connecteur avant de le démarrer. Le doctor de TranscrIA (côté cœur) restera, lui,
un check HTTP de santé — il ne peut pas importer ce package (contrat d'isolation).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ConnectorConfig:
    base_url: str
    api_token: str
    provider: str


@dataclass(frozen=True)
class HealthReport:
    ok: bool
    issues: list[str] = field(default_factory=list)


def validate_config(config: ConnectorConfig) -> HealthReport:
    """Vérifie la config minimale (URL TranscrIA HTTPS/localhost, jeton `tia_`, provider).
    Retourne un rapport lisible plutôt que de lever — le doctor l'affiche."""
    issues: list[str] = []
    url = (config.base_url or "").strip()
    if not url:
        issues.append("base_url manquant (URL de l'API de jobs TranscrIA)")
    elif not (url.startswith("https://") or url.startswith("http://127.0.0.1")
              or url.startswith("http://localhost")):
        issues.append("base_url doit être HTTPS (ou localhost) — un jeton ne transite pas en clair")
    token = (config.api_token or "").strip()
    if not token:
        issues.append("api_token manquant (jeton d'API personnel tia_)")
    elif not token.startswith("tia_"):
        issues.append("api_token doit être un jeton personnel TranscrIA (préfixe tia_)")
    if not (config.provider or "").strip():
        issues.append("provider manquant (visio/zoom/teams/meet)")
    return HealthReport(ok=not issues, issues=issues)

"""Politique PURE de la LLM cloud — aucune base, aucun Flask, testable à sec.

Trois décisions (docs/LLM_CLOUD.md § 3.2) :
- une validation admin est VALIDE si elle vise le fournisseur ET le modèle configurés, avec la
  version COURANTE du texte d'information, et n'est pas révoquée ;
- un utilisateur est INTERDIT dès qu'UN de ses groupes l'est ;
- une soumission qui implique la LLM est refusée (code + message FR qui guide) si le mode cloud est
  actif et que la validation manque, que l'utilisateur est interdit, ou que le job vient d'un
  connecteur sans l'autorisation dédiée. Hors cloud : toujours autorisée.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

# Version du TEXTE d'information affiché dans l'écran d'administration. À incrémenter dès que
# le périmètre de ce qui part (§ 3.2.1) ou les faits par hébergeur changent de sens : toutes
# les validations enregistrées deviennent alors caduques (l'admin relit et revalide).
CLOUD_NOTICE_TEXT_VERSION = "llm-cloud-notice-v1"

GATE_OK = "ok"
GATE_NOT_CLOUD = "not_cloud"
GATE_NO_LLM = "no_llm"
GATE_NOT_ACKNOWLEDGED = "not_acknowledged"
GATE_GROUP_FORBIDDEN = "group_forbidden"
GATE_CONNECTORS_NOT_ALLOWED = "connectors_not_allowed"
GATE_PROVIDER_UNRESOLVED = "provider_unresolved"

_MESSAGES_FR = {
    GATE_NOT_ACKNOWLEDGED: (
        "La LLM d'arbitrage cloud n'a pas encore été validée par un administrateur "
        "(Administration → LLM cloud). Aucune transcription ne part sans cette validation."
    ),
    GATE_GROUP_FORBIDDEN: (
        "Un de vos groupes interdit de confier les transcriptions à un fournisseur externe : "
        "ce traitement est refusé. Rapprochez-vous de votre administrateur."
    ),
    GATE_CONNECTORS_NOT_ALLOWED: (
        "Cette réunion a été captée par un connecteur ou un bot : l'administrateur n'a pas autorisé "
        "ces réunions à partir vers la LLM cloud (Administration → LLM cloud)."
    ),
    GATE_PROVIDER_UNRESOLVED: (
        "Fournisseur de LLM cloud non résolu dans la configuration (model_id « <fournisseur>/<modèle> » "
        "attendu) : traitement refusé."
    ),
}


@dataclass(frozen=True)
class CloudGate:
    """Décision d'admission d'une soumission vis-à-vis de la LLM cloud."""

    allowed: bool
    code: str
    message_fr: str = ""

    @property
    def refused(self) -> bool:
        return not self.allowed


def ack_is_valid(
    *,
    ack_provider: str | None,
    ack_model: str | None,
    ack_text_version: str | None,
    ack_revoked: bool,
    provider: str,
    model: str,
    text_version: str = CLOUD_NOTICE_TEXT_VERSION,
) -> bool:
    """La validation enregistrée couvre-t-elle EXACTEMENT la configuration courante ?"""
    if ack_revoked or not ack_provider or not ack_model:
        return False
    return (
        str(ack_provider).strip().lower() == str(provider or "").strip().lower()
        and str(ack_model).strip() == str(model or "").strip()
        and str(ack_text_version or "") == text_version
    )


def user_forbidden(group_flags: Iterable[bool]) -> bool:
    """Interdit dès qu'UN groupe de l'utilisateur porte l'interdiction (§ 3.2.5)."""
    return any(bool(flag) for flag in group_flags)


def decide_gate(
    *,
    cloud_active: bool,
    needs_llm: bool,
    provider_resolved: bool,
    acknowledged: bool,
    forbidden: bool,
    from_connector: bool,
    connectors_allowed: bool,
) -> CloudGate:
    """Décision PURE, dans l'ordre : hors cloud / sans LLM → ok ; fournisseur irrésolu ;
    validation absente ; groupe interdit ; connecteur non autorisé."""
    if not cloud_active:
        return CloudGate(True, GATE_NOT_CLOUD)
    if not needs_llm:
        return CloudGate(True, GATE_NO_LLM)
    if not provider_resolved:
        return CloudGate(False, GATE_PROVIDER_UNRESOLVED, _MESSAGES_FR[GATE_PROVIDER_UNRESOLVED])
    if not acknowledged:
        return CloudGate(False, GATE_NOT_ACKNOWLEDGED, _MESSAGES_FR[GATE_NOT_ACKNOWLEDGED])
    if forbidden:
        return CloudGate(False, GATE_GROUP_FORBIDDEN, _MESSAGES_FR[GATE_GROUP_FORBIDDEN])
    if from_connector and not connectors_allowed:
        return CloudGate(False, GATE_CONNECTORS_NOT_ALLOWED, _MESSAGES_FR[GATE_CONNECTORS_NOT_ALLOWED])
    return CloudGate(True, GATE_OK)


def job_from_connector(extra_data: dict | None) -> bool:
    """Un job « réunion » (ingestion par connecteur/bot) porte ``extra_data.source == "meeting"``."""
    return str((extra_data or {}).get("source") or "") == "meeting"


def gate_message(code: str) -> str:
    return _MESSAGES_FR.get(code, "")

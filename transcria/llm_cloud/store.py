"""Accès base de la gouvernance LLM cloud : validations admin et interdiction par groupe."""
from __future__ import annotations

import json
from datetime import UTC, datetime

from transcria.auth.models import Group, GroupMembership
from transcria.database import db
from transcria.llm_cloud.models import LlmCloudAcknowledgement
from transcria.llm_cloud.policy import CLOUD_NOTICE_TEXT_VERSION, ack_is_valid, user_forbidden


class LlmCloudStore:
    """Méthodes statiques (patron des autres stores)."""

    @staticmethod
    def current(provider: str, model: str) -> LlmCloudAcknowledgement | None:
        """Validation la plus récente, non révoquée, pour ce couple (fournisseur, modèle)."""
        if not provider or not model:
            return None
        q = (
            db.select(LlmCloudAcknowledgement)
            .filter(
                LlmCloudAcknowledgement.provider == str(provider).strip().lower(),
                LlmCloudAcknowledgement.model == str(model).strip(),
                LlmCloudAcknowledgement.revoked_at.is_(None),
            )
            .order_by(LlmCloudAcknowledgement.acknowledged_at.desc())
            .limit(1)
        )
        return db.session.execute(q).scalars().first()

    @staticmethod
    def latest_any() -> LlmCloudAcknowledgement | None:
        """Dernière validation enregistrée, quel que soit le couple (affichage de l'historique)."""
        q = db.select(LlmCloudAcknowledgement).order_by(LlmCloudAcknowledgement.acknowledged_at.desc()).limit(1)
        return db.session.execute(q).scalars().first()

    @staticmethod
    def is_valid(provider: str, model: str, text_version: str = CLOUD_NOTICE_TEXT_VERSION) -> bool:
        ack = LlmCloudStore.current(provider, model)
        if ack is None:
            return False
        return ack_is_valid(
            ack_provider=ack.provider, ack_model=ack.model, ack_text_version=ack.text_version,
            ack_revoked=ack.revoked_at is not None, provider=provider, model=model,
            text_version=text_version,
        )

    @staticmethod
    def acknowledge(
        provider: str,
        model: str,
        *,
        user_id: str | None,
        username: str,
        connectors_allowed: bool,
        text_version: str = CLOUD_NOTICE_TEXT_VERSION,
        usages: dict | None = None,
    ) -> LlmCloudAcknowledgement:
        """Enregistre une validation (nouvelle ligne ; l'ancienne du même couple est révoquée)."""
        now = datetime.now(UTC)
        previous = LlmCloudStore.current(provider, model)
        if previous is not None:
            previous.revoked_at = now
        ack = LlmCloudAcknowledgement(
            provider=str(provider).strip().lower(),
            model=str(model).strip(),
            text_version=text_version,
            acknowledged_by=user_id,
            acknowledged_by_username=username or "",
            acknowledged_at=now,
            connectors_allowed=bool(connectors_allowed),
            usages_json=json.dumps(usages or {"pipeline": True}, ensure_ascii=False),
        )
        db.session.add(ack)
        db.session.commit()
        return ack

    @staticmethod
    def revoke(provider: str, model: str) -> bool:
        """Révoque la validation courante (l'admin retire son accord) — True si une ligne a changé."""
        ack = LlmCloudStore.current(provider, model)
        if ack is None:
            return False
        ack.revoked_at = datetime.now(UTC)
        db.session.commit()
        return True

    @staticmethod
    def user_forbidden(user_id: str | None) -> bool:
        """L'utilisateur appartient-il à AU MOINS un groupe qui interdit la LLM cloud ?"""
        if not user_id:
            return False
        q = (
            db.select(Group.llm_cloud_forbidden)
            .join(GroupMembership, GroupMembership.group_id == Group.id)
            .filter(GroupMembership.user_id == user_id)
        )
        return user_forbidden(db.session.execute(q).scalars().all())

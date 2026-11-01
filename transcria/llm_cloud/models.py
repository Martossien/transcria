"""Table ``llm_cloud_acknowledgements`` — validation admin de l'usage d'un fournisseur externe.

Append-only : chaque validation ajoute une ligne ; la ligne la plus récente (``revoked_at`` nul)
pour un couple (fournisseur, modèle) est l'actuelle. ``text_version`` est la version du texte
d'information que l'admin a lu : si le texte change, la validation devient CADUQUE (politique
``policy.ack_is_valid``) — jamais de repli silencieux.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from transcria.database import db


class LlmCloudAcknowledgement(db.Model):
    __tablename__ = "llm_cloud_acknowledgements"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    provider = db.Column(db.String(64), nullable=False, index=True)
    model = db.Column(db.String(255), nullable=False)
    text_version = db.Column(db.String(80), nullable=False)
    acknowledged_by = db.Column(db.String(36), db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    acknowledged_by_username = db.Column(db.String(80), nullable=False, default="", server_default="")
    acknowledged_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    # Réunions captées par connecteurs/bots autorisées à partir (défaut : non — § 3.2.3).
    connectors_allowed = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    # Usages couverts par la validation (§ 3.2.7 : {"pipeline": true} aujourd'hui ; « discuter
    # avec mes réunions » ajoutera sa clé sans migration).
    usages_json = db.Column(db.Text, nullable=False, default='{"pipeline": true}',
                            server_default='{"pipeline": true}')
    revoked_at = db.Column(db.DateTime(timezone=True), nullable=True)

    def usages(self) -> dict:
        try:
            data = json.loads(self.usages_json or "{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "provider": self.provider,
            "model": self.model,
            "text_version": self.text_version,
            "acknowledged_by": self.acknowledged_by,
            "acknowledged_by_username": self.acknowledged_by_username,
            "acknowledged_at": self.acknowledged_at.isoformat() if self.acknowledged_at else None,
            "connectors_allowed": bool(self.connectors_allowed),
            "usages": self.usages(),
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
        }

"""Types de réunion personnalisés — modèle SQL (cf. docs/TYPES_REUNION_PERSONNALISES.md §2.2).

Un template = une fiche du MÊME schéma que le catalogue intégré (`definition_json`,
validée par ``meeting_type_catalog.validate_type_definition``), plus une portée de
visibilité décalquée des lexiques centralisés :

- ``private`` : visible du seul créateur (portée de création — tout utilisateur) ;
- ``group``   : visible des membres du groupe (``group_id``), promue par un admin ;
- ``global``  : visible de tous, promue par un admin global.

Le binaire du logo (lot C) vit en colonnes séparées (``logo_blob``/``logo_mime``) :
il ne transite JAMAIS par ``definition_json`` ni par le format d'échange (§8.3) —
le référentiel est en base pour suivre la topologie split (jamais de disque commun).
"""
import json
import uuid
from datetime import datetime, timezone

from transcria.database import db

SCOPE_PRIVATE = "private"
SCOPE_GROUP = "group"
SCOPE_GLOBAL = "global"
SCOPES = (SCOPE_PRIVATE, SCOPE_GROUP, SCOPE_GLOBAL)


class MeetingTypeTemplate(db.Model):
    __tablename__ = "meeting_type_templates"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    slug = db.Column(db.String(80), nullable=False, index=True)
    name = db.Column(db.String(80), nullable=False, index=True)
    definition_json = db.Column(db.Text, nullable=False)
    logo_blob = db.Column(db.LargeBinary, nullable=True)
    logo_mime = db.Column(db.String(40), nullable=False, default="")
    scope = db.Column(db.String(10), nullable=False, default=SCOPE_PRIVATE, index=True)
    group_id = db.Column(db.String(36), db.ForeignKey("groups.id"), nullable=True, index=True)
    created_by = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    group = db.relationship("Group")
    creator = db.relationship("User", foreign_keys=[created_by])

    @property
    def definition(self) -> dict:
        try:
            parsed = json.loads(self.definition_json or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @definition.setter
    def definition(self, value: dict) -> None:
        self.definition_json = json.dumps(value or {}, ensure_ascii=False)

    def to_dict(self, include_definition: bool = True) -> dict:
        data = {
            "id": self.id,
            "slug": self.slug,
            "name": self.name,
            "scope": self.scope,
            "group_id": self.group_id,
            "group_name": self.group.name if self.group else "",
            "created_by": self.created_by,
            "creator_username": self.creator.username if self.creator else "",
            "is_active": self.is_active,
            "has_logo": self.logo_blob is not None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_definition:
            data["definition"] = self.definition
        return data

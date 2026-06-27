import json
import uuid
from datetime import datetime, timezone
from typing import cast

from transcria.database import db


class GroupLexicon(db.Model):
    __tablename__ = "group_lexicons"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    group_id = db.Column(db.String(36), db.ForeignKey("groups.id"), nullable=True, index=True)
    name = db.Column(db.String(160), nullable=False, index=True)
    description = db.Column(db.String(500), nullable=False, default="")
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_by = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    group = db.relationship("Group")
    creator = db.relationship("User", foreign_keys=[created_by])
    entries = db.relationship(
        "GroupLexiconEntry",
        back_populates="lexicon",
        cascade="all, delete-orphan",
        order_by="GroupLexiconEntry.term",
    )

    def to_dict(self, include_entries: bool = False) -> dict:
        data = {
            "id": self.id,
            "group_id": self.group_id,
            "group_name": self.group.name if self.group else "",
            "name": self.name,
            "description": self.description,
            "is_active": self.is_active,
            "entry_count": len(self.entries or []),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_entries:
            data["entries"] = [entry.to_dict() for entry in cast("list[GroupLexiconEntry]", self.entries)]
        return data


class GroupLexiconEntry(db.Model):
    __tablename__ = "group_lexicon_entries"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    lexicon_id = db.Column(db.String(36), db.ForeignKey("group_lexicons.id"), nullable=False, index=True)
    term = db.Column(db.String(255), nullable=False, index=True)
    variants_json = db.Column(db.Text, nullable=False, default="[]")
    category = db.Column(db.String(80), nullable=False, default="mot suspect")
    priority = db.Column(db.String(30), nullable=False, default="normale")
    replace_by = db.Column(db.String(255), nullable=False, default="")
    comment = db.Column(db.String(1000), nullable=False, default="")
    source = db.Column(db.String(40), nullable=False, default="manual")
    usage_count = db.Column(db.Integer, nullable=False, default=0)
    last_used_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    lexicon = db.relationship("GroupLexicon", back_populates="entries")

    @property
    def variants(self) -> list[str]:
        try:
            parsed = json.loads(self.variants_json or "[]")
        except (TypeError, json.JSONDecodeError):
            return []
        return [str(item) for item in parsed if str(item).strip()] if isinstance(parsed, list) else []

    @variants.setter
    def variants(self, value: list[str]) -> None:
        self.variants_json = json.dumps(value or [], ensure_ascii=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "lexicon_id": self.lexicon_id,
            "term": self.term,
            "variants": self.variants,
            "category": self.category,
            "priority": self.priority,
            "replace_by": self.replace_by,
            "comment": self.comment,
            "source": self.source,
            "usage_count": self.usage_count,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

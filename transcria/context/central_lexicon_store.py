import logging
from datetime import datetime, timezone

from sqlalchemy import func

from transcria.auth.groups import GroupStore
from transcria.auth.models import Role, User
from transcria.context.central_lexicon_models import GroupLexicon, GroupLexiconEntry
from transcria.context.central_lexicon_service import normalize_match_text
from transcria.context.lexicon import LEXICON_CATEGORIES, LEXICON_PRIORITIES, LexiconManager
from transcria.context.lexicon_audit import lexicon_entries_audit_summary
from transcria.database import db
from transcria.jobs.models import Job

logger = logging.getLogger(__name__)


def _datetime_sort_value(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min
    if value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


class CentralLexiconAccessError(PermissionError):
    pass


class CentralLexiconValidationError(ValueError):
    pass


class CentralLexiconStore:
    @staticmethod
    def can_manage_lexicons(user: User) -> bool:
        return bool(user and user.is_authenticated and GroupStore.is_group_admin(user))

    @staticmethod
    def can_manage_lexicon(user: User, lexicon: GroupLexicon) -> bool:
        if user.has_role(Role.ADMIN):
            return True
        return lexicon.group_id is not None and GroupStore.can_manage_group(user, lexicon.group_id)

    @staticmethod
    def _validate_group_scope(actor: User, group_id: str | None, allow_global: bool = False) -> str | None:
        group_id = group_id or None
        if group_id is None:
            if allow_global and actor.has_role(Role.ADMIN):
                return None
            raise CentralLexiconValidationError("Un groupe est obligatoire pour ce lexique.")
        if not GroupStore.can_manage_group(actor, group_id):
            raise CentralLexiconAccessError("Accès groupe interdit")
        return group_id

    @staticmethod
    def list_manageable_lexicons(user: User) -> list[GroupLexicon]:
        if user.has_role(Role.ADMIN):
            query = db.select(GroupLexicon).order_by(GroupLexicon.name)
        else:
            group_ids = GroupStore.user_group_ids(user.id, admin_only=True)
            if not group_ids:
                return []
            query = db.select(GroupLexicon).filter(GroupLexicon.group_id.in_(group_ids)).order_by(GroupLexicon.name)
        return list(db.session.execute(query).scalars().all())

    @staticmethod
    def list_accessible_lexicons_for_user(user: User) -> list[GroupLexicon]:
        if user.has_role(Role.ADMIN):
            query = db.select(GroupLexicon).filter_by(is_active=True).order_by(GroupLexicon.name)
        else:
            group_ids = GroupStore.user_group_ids(user.id)
            if group_ids:
                query = (
                    db.select(GroupLexicon)
                    .filter(
                        GroupLexicon.is_active.is_(True),
                        db.or_(GroupLexicon.group_id.is_(None), GroupLexicon.group_id.in_(group_ids)),
                    )
                    .order_by(GroupLexicon.name)
                )
            else:
                query = (
                    db.select(GroupLexicon)
                    .filter(GroupLexicon.is_active.is_(True), GroupLexicon.group_id.is_(None))
                    .order_by(GroupLexicon.name)
                )
        return list(db.session.execute(query).scalars().all())

    @staticmethod
    def list_accessible_lexicons_for_job(job: Job) -> list[GroupLexicon]:
        owner = db.session.get(User, job.owner_id)
        if owner is None:
            return []
        if owner.has_role(Role.ADMIN):
            query = db.select(GroupLexicon).filter_by(is_active=True).order_by(GroupLexicon.name)
        else:
            group_ids = GroupStore.user_group_ids(owner.id)
            if not group_ids:
                query = (
                    db.select(GroupLexicon)
                    .filter(GroupLexicon.is_active.is_(True), GroupLexicon.group_id.is_(None))
                    .order_by(GroupLexicon.name)
                )
            else:
                query = (
                    db.select(GroupLexicon)
                    .filter(
                        GroupLexicon.is_active.is_(True),
                        db.or_(GroupLexicon.group_id.is_(None), GroupLexicon.group_id.in_(group_ids)),
                    )
                    .order_by(GroupLexicon.name)
                )
        return list(db.session.execute(query).scalars().all())

    @staticmethod
    def get_manageable_lexicon(lexicon_id: str, actor: User) -> GroupLexicon | None:
        lexicon = db.session.get(GroupLexicon, lexicon_id)
        if lexicon is None:
            return None
        if not CentralLexiconStore.can_manage_lexicon(actor, lexicon):
            raise CentralLexiconAccessError("Accès lexique interdit")
        return lexicon

    @staticmethod
    def create_lexicon(actor: User, *, name: str, group_id: str | None, description: str = "", allow_global: bool = False) -> GroupLexicon:
        clean_name = name.strip()
        if not clean_name:
            raise CentralLexiconValidationError("Le nom du lexique est obligatoire.")
        group_id = CentralLexiconStore._validate_group_scope(actor, group_id, allow_global=allow_global)
        lexicon = GroupLexicon(
            name=clean_name,
            description=description.strip(),
            group_id=group_id,
            created_by=actor.id,
        )
        db.session.add(lexicon)
        db.session.commit()
        logger.info("Lexique central créé: id=%s group=%s actor=%s", lexicon.id, group_id, actor.id)
        return lexicon

    @staticmethod
    def update_lexicon(
        lexicon: GroupLexicon, actor: User, *, name: str, description: str,
        group_id: str | None, allow_global: bool = False,
    ) -> GroupLexicon:
        if not CentralLexiconStore.can_manage_lexicon(actor, lexicon):
            raise CentralLexiconAccessError("Accès lexique interdit")
        clean_name = name.strip()
        if not clean_name:
            raise CentralLexiconValidationError("Le nom du lexique est obligatoire.")
        group_id = CentralLexiconStore._validate_group_scope(actor, group_id, allow_global=allow_global)
        lexicon.name = clean_name
        lexicon.description = description.strip()
        lexicon.group_id = group_id
        db.session.commit()
        logger.info("Lexique central mis à jour: id=%s group=%s actor=%s", lexicon.id, group_id, actor.id)
        return lexicon

    @staticmethod
    def delete_lexicon(lexicon: GroupLexicon, actor: User) -> None:
        if not CentralLexiconStore.can_manage_lexicon(actor, lexicon):
            raise CentralLexiconAccessError("Accès lexique interdit")
        lexicon_id = lexicon.id
        db.session.delete(lexicon)
        db.session.commit()
        logger.info("Lexique central supprimé: id=%s actor=%s", lexicon_id, actor.id)

    @staticmethod
    def add_or_update_entry(
        lexicon: GroupLexicon,
        actor: User,
        *,
        entry_id: str | None = None,
        term: str,
        variants=None,
        category: str = "mot suspect",
        priority: str = "normale",
        replace_by: str = "",
        comment: str = "",
        source: str = "manual",
    ) -> GroupLexiconEntry:
        if not CentralLexiconStore.can_manage_lexicon(actor, lexicon):
            raise CentralLexiconAccessError("Accès lexique interdit")
        clean_term = term.strip()
        if not clean_term:
            raise CentralLexiconValidationError("La forme correcte est obligatoire.")
        category = category.strip() or "mot suspect"
        if category not in LEXICON_CATEGORIES:
            category = "mot suspect"
        priority = priority.strip() or "normale"
        if priority not in LEXICON_PRIORITIES:
            priority = "normale"
        variants = LexiconManager._normalize_variants(variants or [], term=clean_term)

        duplicate = db.session.execute(
            db.select(GroupLexiconEntry).filter(
                GroupLexiconEntry.lexicon_id == lexicon.id,
                func.lower(GroupLexiconEntry.term) == clean_term.casefold(),
            )
        ).scalars().first()
        if duplicate is not None and duplicate.id != entry_id:
            raise CentralLexiconValidationError("Ce terme existe déjà dans ce lexique.")

        entry = db.session.get(GroupLexiconEntry, entry_id) if entry_id else None
        if entry is None:
            entry = GroupLexiconEntry(lexicon_id=lexicon.id)
            db.session.add(entry)
        elif entry.lexicon_id != lexicon.id:
            raise CentralLexiconAccessError("Entrée hors lexique")

        entry.term = clean_term
        entry.variants = variants
        entry.category = category
        entry.priority = priority
        entry.replace_by = replace_by.strip()
        entry.comment = comment.strip()
        entry.source = source.strip() or "manual"
        db.session.commit()
        logger.info(
            "Entrée lexique central enregistrée: lexicon=%s entry=%s term_len=%d actor=%s",
            lexicon.id,
            entry.id,
            len(entry.term or ""),
            actor.id,
        )
        return entry

    @staticmethod
    def get_entry(lexicon: GroupLexicon, entry_id: str) -> GroupLexiconEntry:
        entry = db.session.get(GroupLexiconEntry, entry_id)
        if entry is None or entry.lexicon_id != lexicon.id:
            raise CentralLexiconValidationError("Entrée introuvable.")
        return entry

    @staticmethod
    def delete_entry(lexicon: GroupLexicon, entry_id: str, actor: User) -> None:
        if not CentralLexiconStore.can_manage_lexicon(actor, lexicon):
            raise CentralLexiconAccessError("Accès lexique interdit")
        entry = CentralLexiconStore.get_entry(lexicon, entry_id)
        db.session.delete(entry)
        db.session.commit()
        logger.info("Entrée lexique central supprimée: lexicon=%s entry=%s actor=%s", lexicon.id, entry_id, actor.id)

    @staticmethod
    def import_entries(lexicon: GroupLexicon, actor: User, content: str) -> dict:
        imported = 0
        rejected = 0
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [part.strip() for part in line.split(",")]
            term = parts[0] if parts else ""
            category = parts[1] if len(parts) > 1 else "mot suspect"
            priority = parts[2] if len(parts) > 2 else "normale"
            comment = parts[3] if len(parts) > 3 else ""
            try:
                CentralLexiconStore.add_or_update_entry(
                    lexicon,
                    actor,
                    term=term,
                    category=category,
                    priority=priority,
                    comment=comment,
                    source="imported",
                )
                imported += 1
            except (CentralLexiconValidationError, CentralLexiconAccessError):
                rejected += 1
        logger.info("Import lexique central: lexicon=%s imported=%d rejected=%d actor=%s", lexicon.id, imported, rejected, actor.id)
        return {"imported": imported, "rejected": rejected}

    @staticmethod
    def entries_for_lexicons(lexicons: list[GroupLexicon]) -> list[dict]:
        entries: list[dict] = []
        for lexicon in lexicons:
            for entry in lexicon.entries:
                data = entry.to_dict()
                data["source"] = "central"
                data["central_entry_id"] = entry.id
                data["central_lexicon_id"] = lexicon.id
                data["central_lexicon_name"] = lexicon.name
                entries.append(data)
        return entries

    @staticmethod
    def usage_stats(lexicon: GroupLexicon) -> dict:
        entries = list(lexicon.entries or [])
        total_usage = sum(int(entry.usage_count or 0) for entry in entries)
        used_entries = [entry for entry in entries if int(entry.usage_count or 0) > 0]
        last_used_dates = [entry.last_used_at for entry in used_entries if entry.last_used_at]
        top_entries = sorted(
            used_entries,
            key=lambda entry: (int(entry.usage_count or 0), _datetime_sort_value(entry.last_used_at), entry.term.casefold()),
            reverse=True,
        )[:5]
        never_used = [entry for entry in entries if int(entry.usage_count or 0) == 0]
        return {
            "entry_count": len(entries),
            "total_usage": total_usage,
            "used_count": len(used_entries),
            "never_used_count": len(never_used),
            "last_used_at": max(last_used_dates, key=_datetime_sort_value) if last_used_dates else None,
            "top_entries": top_entries,
            "never_used_entries": never_used[:8],
        }

    @staticmethod
    def sensitivity_summary(lexicon: GroupLexicon) -> dict:
        return lexicon_entries_audit_summary(lexicon.entries or [])

    @staticmethod
    def quality_issues(lexicon: GroupLexicon) -> list[dict]:
        issues: list[dict] = []
        entries = list(lexicon.entries or [])
        normalized_terms: dict[str, list[GroupLexiconEntry]] = {}
        for entry in entries:
            normalized = normalize_match_text(entry.term)
            normalized_terms.setdefault(normalized, []).append(entry)

            if len(normalized) <= 2:
                issues.append({
                    "severity": "warning",
                    "entry": entry,
                    "message": "Terme très court : risque de faux positifs.",
                })

            for variant in entry.variants:
                if normalize_match_text(variant) == normalized:
                    issues.append({
                        "severity": "info",
                        "entry": entry,
                        "message": "Variante identique à la forme correcte.",
                    })
                    break

            if not entry.variants and entry.priority == "normale" and int(entry.usage_count or 0) == 0:
                issues.append({
                    "severity": "info",
                    "entry": entry,
                    "message": "Entrée normale sans variante et jamais utilisée.",
                })

        for similar_entries in normalized_terms.values():
            if len(similar_entries) <= 1:
                continue
            terms = ", ".join(entry.term for entry in similar_entries[:4])
            issues.append({
                "severity": "warning",
                "entry": similar_entries[0],
                "message": f"Doublons proches détectés : {terms}.",
            })

        return issues[:20]

    @staticmethod
    def mark_entries_used(entry_ids: list[str]) -> None:
        if not entry_ids:
            return
        now = datetime.now(timezone.utc)
        entries = db.session.execute(
            db.select(GroupLexiconEntry).filter(GroupLexiconEntry.id.in_(entry_ids))
        ).scalars().all()
        for entry in entries:
            entry.usage_count += 1
            entry.last_used_at = now
        db.session.commit()
        logger.info("Usage lexique central incrémenté: entries=%d", len(entries))

"""Store des types de réunion personnalisés — RBAC, quotas, collisions, vues fusionnées.

Règles (décision D2 du cadrage) : **tout utilisateur crée** (portée ``private``) ;
**les admins partagent** — un admin de groupe promeut vers SES groupes (y compris un
type privé d'un membre de ses groupes, qu'il peut lister), un admin global promeut en
``global``. Un type partagé ne se modifie plus que par un admin de sa portée (un membre
simple le DUPLIQUE au lieu de l'éditer).

Interdit structurel : masquer un type intégré — ni le nom ni le slug d'un template ne
peuvent entrer en collision avec le catalogue intégré, ni avec un template déjà visible
du créateur (jamais d'ambiguïté dans le menu de l'étape 4).
"""
from __future__ import annotations

import logging
import re
import unicodedata

from transcria.auth.groups import GroupStore
from transcria.auth.models import Role, User
from transcria.context.meeting_type_catalog import (
    SCHEMA_VERSION,
    MeetingTypeCatalogError,
    meeting_type_names,
    type_specific_fields,
    validate_type_definition,
)
from transcria.context.meeting_type_models import (
    SCOPE_GLOBAL,
    SCOPE_GROUP,
    SCOPE_PRIVATE,
    SCOPES,
    MeetingTypeTemplate,
)
from transcria.database import db

logger = logging.getLogger(__name__)

DEFAULT_MAX_PER_USER = 20
MAX_LOGO_BYTES = 500 * 1024          # taille max du fichier téléversé
LOGO_MAX_SIZE = (600, 200)           # le logo est réduit à ces dimensions (pixels)


class MeetingTypeAccessError(PermissionError):
    pass


class MeetingTypeValidationError(ValueError):
    pass


def slugify(name: str) -> str:
    """Slug stable depuis le nom (minuscules ASCII, tirets) — identité d'échange."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return slug[:80] or "type"


def _builtin_slugs() -> set[str]:
    return {slugify(name) for name in meeting_type_names()}


class MeetingTypeStore:
    # ── Visibilité ────────────────────────────────────────────────────────────

    @staticmethod
    def visible_templates_for_user(user: User) -> list[MeetingTypeTemplate]:
        """Templates ACTIFS visibles : les miens (privés) + mes groupes + globaux.

        C'est la vue de l'étape 4 — comme les lexiques, elle se calcule pour le
        PROPRIÉTAIRE du job, jamais pour l'admin qui consulte.
        """
        group_ids = GroupStore.user_group_ids(user.id)
        clauses = [
            db.and_(MeetingTypeTemplate.scope == SCOPE_PRIVATE, MeetingTypeTemplate.created_by == user.id),
            MeetingTypeTemplate.scope == SCOPE_GLOBAL,
        ]
        if group_ids:
            clauses.append(
                db.and_(MeetingTypeTemplate.scope == SCOPE_GROUP, MeetingTypeTemplate.group_id.in_(group_ids))
            )
        query = (
            db.select(MeetingTypeTemplate)
            .filter(MeetingTypeTemplate.is_active.is_(True), db.or_(*clauses))
            .order_by(MeetingTypeTemplate.name)
        )
        return list(db.session.execute(query).scalars().all())

    @staticmethod
    def gallery_templates_for_user(user: User) -> list[MeetingTypeTemplate]:
        """Vue de la GALERIE : les visibles (actifs) + MES types inactifs (imports
        « à relire ») — un import invisible de son propre auteur serait introuvable."""
        templates = {t.id: t for t in MeetingTypeStore.visible_templates_for_user(user)}
        own = db.session.execute(
            db.select(MeetingTypeTemplate).filter_by(created_by=user.id)
        ).scalars().all()
        for template in own:
            templates.setdefault(template.id, template)
        return sorted(templates.values(), key=lambda t: t.name)

    @staticmethod
    def list_manageable(user: User) -> list[MeetingTypeTemplate]:
        """Vue de gestion : les miens + (admin de groupe) ceux de mes groupes et les
        privés des MEMBRES de mes groupes (pour pouvoir les partager) + (admin) tous."""
        if user.has_role(Role.ADMIN):
            query = db.select(MeetingTypeTemplate).order_by(MeetingTypeTemplate.name)
            return list(db.session.execute(query).scalars().all())
        clauses = [MeetingTypeTemplate.created_by == user.id]
        admin_group_ids = GroupStore.user_group_ids(user.id, admin_only=True)
        if admin_group_ids:
            clauses.append(
                db.and_(MeetingTypeTemplate.scope == SCOPE_GROUP, MeetingTypeTemplate.group_id.in_(admin_group_ids))
            )
            member_ids = {
                membership.user_id
                for group_id in admin_group_ids
                for membership in GroupStore.list_members(group_id)
            }
            if member_ids:
                clauses.append(
                    db.and_(
                        MeetingTypeTemplate.scope == SCOPE_PRIVATE,
                        MeetingTypeTemplate.created_by.in_(member_ids),
                    )
                )
        query = db.select(MeetingTypeTemplate).filter(db.or_(*clauses)).order_by(MeetingTypeTemplate.name)
        return list(db.session.execute(query).scalars().all())

    @staticmethod
    def get(template_id: str) -> MeetingTypeTemplate | None:
        return db.session.get(MeetingTypeTemplate, template_id)

    @staticmethod
    def resolve_for_user(user: User, name: str) -> MeetingTypeTemplate | None:
        """Le template visible de ``user`` portant ce nom (résolution étape 4)."""
        for template in MeetingTypeStore.visible_templates_for_user(user):
            if template.name == name:
                return template
        return None

    # ── Droits ────────────────────────────────────────────────────────────────

    @staticmethod
    def can_manage(user: User, template: MeetingTypeTemplate) -> bool:
        if user.has_role(Role.ADMIN):
            return True
        if template.scope == SCOPE_PRIVATE:
            return template.created_by == user.id or MeetingTypeStore._is_admin_of_creator(user, template)
        if template.scope == SCOPE_GROUP:
            return template.group_id is not None and GroupStore.can_manage_group(user, template.group_id)
        return False  # global : admin global uniquement

    @staticmethod
    def _is_admin_of_creator(user: User, template: MeetingTypeTemplate) -> bool:
        admin_group_ids = GroupStore.user_group_ids(user.id, admin_only=True)
        if not admin_group_ids:
            return False
        creator_group_ids = set(GroupStore.user_group_ids(template.created_by))
        return bool(creator_group_ids & set(admin_group_ids))

    # ── Collisions & quota ────────────────────────────────────────────────────

    @staticmethod
    def _check_collisions(creator: User, name: str, slug: str, exclude_id: str | None = None) -> None:
        if name in meeting_type_names() or slug in _builtin_slugs():
            raise MeetingTypeValidationError(
                f"« {name} » entre en collision avec un type intégré — choisissez un autre nom."
            )
        for template in MeetingTypeStore.visible_templates_for_user(creator):
            if template.id == exclude_id:
                continue
            if template.name == name or template.slug == slug:
                raise MeetingTypeValidationError(
                    f"« {name} » existe déjà dans vos types visibles — choisissez un autre nom."
                )

    @staticmethod
    def _check_quota(creator: User, max_per_user: int) -> None:
        count = db.session.execute(
            db.select(db.func.count()).select_from(MeetingTypeTemplate).filter_by(created_by=creator.id)
        ).scalar_one()
        if count >= max_per_user:
            raise MeetingTypeValidationError(
                f"Quota atteint ({max_per_user} types par utilisateur) — supprimez un type inutilisé."
            )

    # ── CRUD ──────────────────────────────────────────────────────────────────

    @staticmethod
    def create_template(actor: User, raw_definition: dict, *, max_per_user: int = DEFAULT_MAX_PER_USER) -> MeetingTypeTemplate:
        """Création — toujours en portée ``private`` (le partage est un acte d'admin)."""
        try:
            definition = validate_type_definition(raw_definition)
        except MeetingTypeCatalogError as exc:
            raise MeetingTypeValidationError(str(exc)) from exc
        slug = slugify(definition["name"])
        MeetingTypeStore._check_quota(actor, max_per_user)
        MeetingTypeStore._check_collisions(actor, definition["name"], slug)
        template = MeetingTypeTemplate(
            slug=slug,
            name=definition["name"],
            scope=SCOPE_PRIVATE,
            created_by=actor.id,
        )
        template.definition = definition
        db.session.add(template)
        db.session.commit()
        return template

    @staticmethod
    def update_template(actor: User, template_id: str, raw_definition: dict) -> MeetingTypeTemplate:
        template = MeetingTypeStore.get(template_id)
        if template is None:
            raise MeetingTypeValidationError("Type introuvable.")
        if not MeetingTypeStore.can_manage(actor, template):
            raise MeetingTypeAccessError("Vous ne pouvez pas modifier ce type (dupliquez-le).")
        try:
            definition = validate_type_definition(raw_definition)
        except MeetingTypeCatalogError as exc:
            raise MeetingTypeValidationError(str(exc)) from exc
        slug = slugify(definition["name"])
        creator = db.session.get(User, template.created_by) or actor
        MeetingTypeStore._check_collisions(creator, definition["name"], slug, exclude_id=template.id)
        template.name = definition["name"]
        template.slug = slug
        template.definition = definition
        template.is_active = True   # un type importé (inactif) est activé par sa relecture
        db.session.commit()
        return template

    @staticmethod
    def delete_template(actor: User, template_id: str) -> None:
        template = MeetingTypeStore.get(template_id)
        if template is None:
            raise MeetingTypeValidationError("Type introuvable.")
        if not MeetingTypeStore.can_manage(actor, template):
            raise MeetingTypeAccessError("Vous ne pouvez pas supprimer ce type.")
        # Les jobs existants gardent leur fiche MATÉRIALISÉE (context/meeting_context.json)
        # — supprimer le template ne casse aucun rendu passé (cf. cadrage §2.3).
        db.session.delete(template)
        db.session.commit()

    @staticmethod
    def change_scope(actor: User, template_id: str, scope: str, group_id: str | None = None) -> MeetingTypeTemplate:
        """Promotion/rétrogradation — l'acte de PARTAGE, réservé aux admins."""
        template = MeetingTypeStore.get(template_id)
        if template is None:
            raise MeetingTypeValidationError("Type introuvable.")
        if scope not in SCOPES:
            raise MeetingTypeValidationError(f"Portée inconnue : {scope!r}.")
        if scope == SCOPE_GLOBAL and not actor.has_role(Role.ADMIN):
            raise MeetingTypeAccessError("Seul un admin global partage un type à tous.")
        if scope == SCOPE_GROUP:
            if not group_id or not GroupStore.can_manage_group(actor, group_id):
                raise MeetingTypeAccessError("Partage réservé à un admin du groupe cible.")
        if scope == SCOPE_PRIVATE and not MeetingTypeStore.can_manage(actor, template):
            raise MeetingTypeAccessError("Vous ne pouvez pas retirer ce partage.")
        # Pour PROMOUVOIR un type (private→group/global), il faut pouvoir le gérer.
        if template.scope == SCOPE_PRIVATE and scope != SCOPE_PRIVATE \
                and not MeetingTypeStore.can_manage(actor, template):
            raise MeetingTypeAccessError("Vous ne pouvez pas partager ce type.")
        template.scope = scope
        template.group_id = group_id if scope == SCOPE_GROUP else None
        db.session.commit()
        return template

    # ── Format d'échange (export/import + communauté — cadrage §8) ────────────

    @staticmethod
    def export_definition(template: MeetingTypeTemplate) -> dict:
        """Fichier d'échange d'un type : la fiche SANS branding ni binaire (§8.3).

        C'est le MÊME schéma que le catalogue intégré, enveloppé avec sa version —
        un type exporté est directement contribuable à ``community/meeting-types/``.
        """
        definition = dict(template.definition)
        definition.pop("branding", None)
        return {"schema_version": SCHEMA_VERSION, "type": definition}

    @staticmethod
    def import_definition(actor: User, payload: object, *,
                          max_per_user: int = DEFAULT_MAX_PER_USER) -> MeetingTypeTemplate:
        """Import d'un fichier d'échange → type PRIVÉ et INACTIF (« à relire »).

        Refus EXPLICITES, jamais de nettoyage silencieux (§8.2) : enveloppe et
        ``schema_version`` obligatoires ; ``branding`` interdit (local à chaque
        installation) ; la fiche passe la validation complète du catalogue.
        Collision de nom → suffixe « (import) », jamais d'écrasement.
        """
        if not isinstance(payload, dict) or set(payload) != {"schema_version", "type"}:
            raise MeetingTypeValidationError(
                "Fichier d'échange invalide : objet {schema_version, type} attendu."
            )
        if payload["schema_version"] != SCHEMA_VERSION:
            raise MeetingTypeValidationError(
                f"schema_version {payload['schema_version']!r} non supporté (attendu {SCHEMA_VERSION})."
            )
        raw = payload["type"]
        if isinstance(raw, dict) and raw.get("branding"):
            raise MeetingTypeValidationError(
                "Le fichier contient un branding (pied de page/logo) : il est local à chaque "
                "installation — retirez la clé 'branding' avant l'import."
            )
        try:
            definition = validate_type_definition(raw)
        except MeetingTypeCatalogError as exc:
            raise MeetingTypeValidationError(str(exc)) from exc

        base_name = definition["name"]
        candidate = base_name
        for attempt in range(2, 10):
            try:
                MeetingTypeStore._check_collisions(actor, candidate, slugify(candidate))
                break
            except MeetingTypeValidationError:
                candidate = f"{base_name} (import{'' if attempt == 2 else f' {attempt - 1}'})"
        definition["name"] = candidate
        MeetingTypeStore._check_quota(actor, max_per_user)
        MeetingTypeStore._check_collisions(actor, candidate, slugify(candidate))
        template = MeetingTypeTemplate(
            slug=slugify(candidate),
            name=candidate,
            scope=SCOPE_PRIVATE,
            created_by=actor.id,
            is_active=False,   # « à relire avant activation » — l'édition + enregistrement active
        )
        template.definition = definition
        db.session.add(template)
        db.session.commit()
        return template

    # ── Logo (branding LOCAL — jamais dans la fiche ni dans l'export, §8.3) ───

    @staticmethod
    def set_logo(actor: User, template_id: str, raw: bytes) -> MeetingTypeTemplate:
        """Pose le logo : image PNG/JPEG ≤ 500 Ko, RE-ENCODÉE via Pillow (dimensions
        bornées, métadonnées EXIF supprimées) — jamais le binaire d'origine."""
        import io

        template = MeetingTypeStore.get(template_id)
        if template is None:
            raise MeetingTypeValidationError("Type introuvable.")
        if not MeetingTypeStore.can_manage(actor, template):
            raise MeetingTypeAccessError("Vous ne pouvez pas modifier ce type.")
        if not raw or len(raw) > MAX_LOGO_BYTES:
            raise MeetingTypeValidationError(f"Logo requis, {MAX_LOGO_BYTES // 1024} Ko maximum.")
        try:
            from PIL import Image

            source = Image.open(io.BytesIO(raw))
            source.load()
            if source.format not in ("PNG", "JPEG"):
                raise MeetingTypeValidationError("Format de logo accepté : PNG ou JPEG.")
            image = source.convert("RGBA")
            image.thumbnail(LOGO_MAX_SIZE)
            out = io.BytesIO()
            image.save(out, format="PNG")
        except MeetingTypeValidationError:
            raise
        except Exception as exc:  # Pillow lève des types variés sur image corrompue
            raise MeetingTypeValidationError("Image de logo illisible.") from exc
        template.logo_blob = out.getvalue()
        template.logo_mime = "image/png"
        db.session.commit()
        return template

    @staticmethod
    def clear_logo(actor: User, template_id: str) -> MeetingTypeTemplate:
        template = MeetingTypeStore.get(template_id)
        if template is None:
            raise MeetingTypeValidationError("Type introuvable.")
        if not MeetingTypeStore.can_manage(actor, template):
            raise MeetingTypeAccessError("Vous ne pouvez pas modifier ce type.")
        template.logo_blob = None
        template.logo_mime = ""
        db.session.commit()
        return template

    # ── Vues fusionnées (étape 4 & rendu) ─────────────────────────────────────

    @staticmethod
    def merged_catalog_for_user(user: User) -> tuple[list[str], list[str], dict[str, list[dict]]]:
        """(noms intégrés ordonnés, noms personnalisés visibles, champs fusionnés)."""
        builtin_names = meeting_type_names()
        templates = MeetingTypeStore.visible_templates_for_user(user)
        custom_names = [t.name for t in templates]
        fields = dict(type_specific_fields())
        for template in templates:
            template_fields = template.definition.get("fields") or []
            if template_fields:
                fields[template.name] = template_fields
        return builtin_names, custom_names, fields

"""Provisionnement à la volée (JIT) des identités fédérées — §3.5 du plan.

Algorithme verrouillé par tests :

1. rapprochement par (source, subject) — JAMAIS l'email ni le username ;
2. compte existant : resynchronisation nom/email, rôle RECALCULÉ (il peut
   baisser — la vérité vient du fournisseur) ; ``is_active=False`` local est un
   VETO même si le fournisseur connaît encore l'utilisateur ;
3. compte nouveau : mapping d'abord (deny → refus AUDITÉ avec les groupes
   reçus), collision de username avec un compte existant → suffixe ``@source``
   (jamais d'écrasement ni de fusion silencieuse) ;
4. mot de passe INUTILISABLE par construction (sentinelle « ! » : aucun hachage
   ne la produit) — les chemins mot-de-passe refusent déjà si source ≠ local.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from transcria.auth.identity.base import FederatedIdentity
from transcria.auth.identity.mapping import MappingDecision, resolve_role
from transcria.auth.models import User, db
from transcria.auth.store import UserStore

logger = logging.getLogger(__name__)

#: Jamais produit par le hacheur (les hachages commencent par leur schéma) :
#: check_password est faux par construction pour un compte fédéré.
UNUSABLE_PASSWORD_HASH = "!federated-account-no-local-password"


class FederatedLoginDenied(Exception):
    """Accès refusé par le mapping (ou veto local) — l'appelant audite et affiche
    le message i18n générique « accès non attribué »."""

    def __init__(self, reason: str, decision: MappingDecision | None = None):
        super().__init__(reason)
        self.reason = reason
        self.decision = decision


def provision_federated(identity: FederatedIdentity, config: dict) -> tuple[User, MappingDecision]:
    """Retourne (compte prêt pour ``login_user``, décision de mapping pour l'audit).

    Lève ``FederatedLoginDenied`` si le mapping refuse ou si le compte local est
    désactivé (veto). Toute écriture est commitée ici — l'appelant n'a rien à
    faire d'autre que la session et l'audit."""
    mapping_cfg = ((config.get("auth", {}) or {}).get("role_mapping", {}) or {})
    decision = resolve_role(identity.groups, mapping_cfg)
    if decision.denied or decision.role is None:
        raise FederatedLoginDenied("aucun groupe mappé (default=deny)", decision)

    user = UserStore.get_by_external(identity.source, identity.subject)
    now = datetime.now(timezone.utc)
    if user is not None:
        if not user.is_active:
            # Veto LOCAL : un admin a désactivé ce compte — le fournisseur ne le
            # réactive pas tout seul (ce serait contourner une décision locale).
            raise FederatedLoginDenied("compte désactivé localement (veto)", decision)
        user.display_name = identity.display_name or user.display_name
        user.email = identity.email or user.email
        user.role = decision.role.value          # REMPLACÉ — peut baisser
        user.last_identity_sync = now
        db.session.commit()
        return user, decision

    username = _unique_username(identity.username or identity.subject, identity.source)
    user = User(
        username=username,
        display_name=identity.display_name or username,
        email=identity.email or "",
        password_hash=UNUSABLE_PASSWORD_HASH,
        role=decision.role.value,
        identity_source=identity.source,
        external_subject=identity.subject,
        last_identity_sync=now,
    )
    db.session.add(user)
    db.session.commit()
    logger.info("JIT : compte fédéré créé (source=%s, username=%s, role=%s)",
                identity.source, username, decision.role.value)
    # Traçabilité PSSI/DPO : un compte apparaît SANS action d'un administrateur
    # (provisionné depuis l'annuaire) — événement distinct du simple LOGIN.
    from transcria.audit.decorator import audit_log
    from transcria.audit.models import AuditAction

    audit_log(AuditAction.USER_PROVISIONED, target_type="user", target_id=user.id,
              target_label=username, details={"source": identity.source,
                                              "role": decision.role.value,
                                              "matched_group": decision.matched_group})
    return user, decision


def _unique_username(base: str, source: str) -> str:
    """Collision avec un compte EXISTANT (local ou autre source) → suffixe
    ``@source`` puis ``@source-2``… — jamais d'écrasement."""
    candidate = base.strip() or f"user@{source}"
    if UserStore.get_by_username(candidate) is None:
        return candidate
    suffixed = f"{candidate}@{source}"
    n = 1
    while UserStore.get_by_username(suffixed) is not None:
        n += 1
        suffixed = f"{candidate}@{source}-{n}"
    return suffixed

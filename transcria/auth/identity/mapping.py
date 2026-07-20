"""Mapping groupes fédérés → rôle du portail (docs/GESTION_IDENTITE.md §3.6).

Module PUR (aucune I/O) — commun aux connecteurs OIDC (claim ``groups``),
LDAP (``memberOf``) et proxy (``Remote-Groups``). Sémantique verrouillée par
tests :

- règles ORDONNÉES, premier match gagne, égalité STRICTE de chaîne (l'admin
  écrit exactement ce que son fournisseur émet — nom court Keycloak, DN
  complet AD, ID de groupe Entra) ;
- aucun match → ``default`` : ``deny`` (refus) ou ``viewer`` — rien d'autre ;
- le rôle est REMPLACÉ à chaque login (jamais ``max(ancien, nouveau)``) : la
  vérité vient du fournisseur, un retrait de groupe rétrograde immédiatement.
"""
from __future__ import annotations

from dataclasses import dataclass

from transcria.auth.models import Role

#: Valeurs acceptées pour ``default`` — ``deny`` refuse, ``viewer`` donne le
#: rôle le plus faible. JAMAIS operator/admin par défaut (élévation implicite).
_ALLOWED_DEFAULTS = ("deny", "viewer")


@dataclass(frozen=True)
class MappingDecision:
    """Résultat du mapping — ``role`` est None si l'accès est refusé.

    ``matched_group`` (ou None) et ``received_groups`` alimentent l'AUDIT : le
    refus « aucun groupe mappé » doit montrer à l'admin ce que le fournisseur a
    réellement émis — c'est son outil de diagnostic n°1."""

    role: Role | None
    matched_group: str | None
    received_groups: tuple[str, ...]

    @property
    def denied(self) -> bool:
        return self.role is None


def resolve_role(groups: tuple[str, ...] | list[str], mapping_cfg: dict) -> MappingDecision:
    """Applique ``auth.role_mapping`` aux groupes reçus. Voir contrat du module."""
    received = tuple(str(g) for g in (groups or ()))
    rules = (mapping_cfg or {}).get("rules") or []
    received_set = set(received)
    for rule in rules:
        group = str((rule or {}).get("group") or "")
        if group and group in received_set:
            role = _coerce_role((rule or {}).get("role"))
            if role is not None:
                return MappingDecision(role=role, matched_group=group, received_groups=received)
    default = str((mapping_cfg or {}).get("default") or "deny").strip().lower()
    if default == "viewer":
        return MappingDecision(role=Role.VIEWER, matched_group=None, received_groups=received)
    return MappingDecision(role=None, matched_group=None, received_groups=received)


def validate_role_mapping(mapping_cfg: dict) -> list[str]:
    """Erreurs de configuration du mapping (consommé par config_schema).

    Contrôles : ``default`` dans l'allowlist ; chaque règle a ``group`` non vide
    et ``role`` existant dans ``Role`` ; les règles sont une liste."""
    errors: list[str] = []
    cfg = mapping_cfg or {}
    default = str(cfg.get("default") or "deny").strip().lower()
    if default not in _ALLOWED_DEFAULTS:
        errors.append(
            f"auth.role_mapping.default='{default}' invalide (accepté : {', '.join(_ALLOWED_DEFAULTS)})"
        )
    rules = cfg.get("rules")
    if rules is None:
        return errors
    if not isinstance(rules, list):
        errors.append("auth.role_mapping.rules doit être une liste")
        return errors
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict) or not str(rule.get("group") or "").strip():
            errors.append(f"auth.role_mapping.rules[{i}] : champ 'group' requis (non vide)")
            continue
        if _coerce_role(rule.get("role")) is None:
            valid = ", ".join(r.value for r in Role)
            errors.append(
                f"auth.role_mapping.rules[{i}] : role='{rule.get('role')}' inconnu (accepté : {valid})"
            )
    return errors


def _coerce_role(value) -> Role | None:
    try:
        return Role(str(value).strip().lower())
    except (ValueError, AttributeError):
        return None

"""Adresse e-mail → identifiant Cloud Identity, seul accepté comme cible d'abonnement.

POURQUOI CE MODULE EXISTE. Un abonnement Workspace Events ne vise qu'un utilisateur ou un
espace, et l'utilisateur se désigne par un IDENTIFIANT (`users/118427905513870264891`),
jamais par son adresse — vérifié en conditions réelles : l'adresse et le `permissionId` de
Drive sont tous deux refusés en 403. Il faut donc traduire, et la traduction exige une portée
que le connecteur n'a pas par défaut.

DEUX VOIES, ET ELLES NE SONT PAS INTERCHANGEABLES À L'ÉCHELLE :

- **l'annuaire** (`admin.directory.user.readonly`) : on impersonne l'ADMINISTRATEUR et on
  demande la fiche d'un utilisateur. Un appel par personne, mais un seul jeton pour tous —
  et c'est la source de vérité de l'organisation ;
- **OpenID** (`openid`) : on impersonne CHAQUE personne et on lit son propre `sub`. Portée
  minuscule, mais un échange de jeton par utilisateur : à cent personnes, cent
  authentifications là où l'annuaire n'en demande qu'une.

L'annuaire est donc essayé d'abord, OpenID sert de repli — utile là où une DSI refuse la
lecture de l'annuaire, ce qui est fréquent et légitime.

⚠ La documentation Google n'affirme NULLE PART que le `sub` OpenID est l'identifiant de
l'annuaire. C'est probable (même espace de nommage que People API), mais probable ne suffit
pas : `verify_resolvers_agree()` existe pour le CONSTATER sur un compte réel, et le repli ne
doit pas être considéré comme équivalent avant que ce constat ait été fait.

Tout est pur ou injecté : aucun réseau ici, le transport vient de l'appelant.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

DIRECTORY_SCOPE = "https://www.googleapis.com/auth/admin.directory.user.readonly"
OPENID_SCOPE = "openid"

DIRECTORY_BASE = "https://admin.googleapis.com/admin/directory/v1/users"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


class UserResolutionError(RuntimeError):
    """Adresse non résolue — le message dit laquelle et pourquoi."""


def explain_failure(detail: str) -> str:
    """Refus brut de Google → cause ACTIONNABLE.

    Deux prérequis se ressemblent dans le message et n'appellent pas du tout le même geste,
    l'un dans la console Cloud, l'autre dans la console d'administration Workspace :

    - l'API n'est pas ACTIVÉE dans le projet (« has not been used in project … before ») —
      constaté le 2026-08-01, alors que la portée était pourtant bien déléguée ;
    - la portée n'est pas DÉLÉGUÉE au compte de service.

    Sans cette distinction, on cherche pendant une heure du mauvais côté.
    """
    brut = str(detail)
    if "has not been used in project" in brut or "is disabled" in brut:
        return ("l'API Admin SDK n'est pas ACTIVÉE dans le projet Cloud — console Google "
                "Cloud → API et services → activer « Admin SDK API » (la délégation, elle, "
                "est indépendante et peut être correcte)")
    if "unauthorized_client" in brut or "not authorized for any of the scopes" in brut:
        return (f"portée non déléguée — ajouter « {DIRECTORY_SCOPE} » à la ligne de "
                f"délégation du compte de service (console d'administration Workspace)")
    return brut


def directory_call(email: str) -> tuple[str, str, None]:
    """(méthode, URL, corps) de la fiche annuaire. PURE.

    `userKey` accepte l'adresse principale, un alias, ou l'identifiant — donc l'adresse
    telle que le portail la connaît, sans traduction préalable.
    """
    import urllib.parse

    adresse = (email or "").strip()
    if "@" not in adresse:
        raise UserResolutionError(f"« {adresse} » n'est pas une adresse e-mail")
    return "GET", f"{DIRECTORY_BASE}/{urllib.parse.quote(adresse)}", None


def userinfo_call() -> tuple[str, str, None]:
    """(méthode, URL, corps) du profil OpenID de l'utilisateur IMPERSONNÉ. PURE."""
    return "GET", USERINFO_URL, None


def user_id_of_directory(payload: Any) -> str:
    if not isinstance(payload, dict) or not payload.get("id"):
        raise UserResolutionError("fiche annuaire sans identifiant exploitable")
    return str(payload["id"])


def user_id_of_userinfo(payload: Any) -> str:
    if not isinstance(payload, dict) or not payload.get("sub"):
        raise UserResolutionError("profil OpenID sans « sub »")
    return str(payload["sub"])


class UserResolver:
    """Traduit les adresses, avec CACHE et repli. Les appels réseau sont injectés.

    Le cache n'est pas une optimisation gratuite : un identifiant Cloud Identity ne change
    jamais (« never reused », dit la documentation), et re-résoudre cent adresses à chaque
    tour de service serait cent authentifications par heure pour une information immuable.
    """

    def __init__(self, *, directory=None, openid=None) -> None:
        # `directory(email) -> payload` et `openid(email) -> payload` : deux fonctions qui
        # font l'appel avec le bon jeton. Absentes = voie indisponible (portée non accordée).
        self._directory = directory
        self._openid = openid
        self._cache: dict[str, str] = {}

    def resolve(self, email: str) -> str:
        adresse = (email or "").strip().lower()
        if not adresse:
            raise UserResolutionError("adresse vide")
        if adresse in self._cache:
            return self._cache[adresse]
        erreurs: list[str] = []
        for nom, appel, lecture in (("annuaire", self._directory, user_id_of_directory),
                                    ("OpenID", self._openid, user_id_of_userinfo)):
            if appel is None:
                continue
            try:
                identifiant = lecture(appel(adresse))
            except Exception as exc:  # noqa: BLE001 — on essaie la voie suivante
                erreurs.append(f"{nom} : {explain_failure(exc)}")
                continue
            self._cache[adresse] = identifiant
            return identifiant
        if not erreurs:
            raise UserResolutionError(
                f"{adresse} : aucune voie de résolution disponible — ajouter la portée "
                f"« {DIRECTORY_SCOPE} » ou « {OPENID_SCOPE} » à la délégation de domaine")
        raise UserResolutionError(f"{adresse} — " + " ; ".join(erreurs))

    def forget(self, email: str) -> None:
        """Oublie une adresse — utile quand un compte est recréé (identifiant différent)."""
        self._cache.pop((email or "").strip().lower(), None)


def verify_resolvers_agree(resolver_directory, resolver_openid, email: str) -> tuple[bool, str]:
    """Les deux voies rendent-elles le MÊME identifiant ? (constat, pas supposition)

    La documentation ne l'affirme pas. Tant que ce n'est pas constaté sur un compte réel, le
    repli OpenID ne doit pas être présenté comme équivalent à l'annuaire : un identifiant
    différent produirait des abonnements qui ne remontent jamais rien — la pire des pannes,
    celle qui se tait.
    """
    try:
        via_annuaire = resolver_directory.resolve(email)
        via_openid = resolver_openid.resolve(email)
    except UserResolutionError as exc:
        return False, f"comparaison impossible : {exc}"
    if via_annuaire == via_openid:
        return True, f"les deux voies s'accordent sur {via_annuaire}"
    return False, (f"DIVERGENCE — annuaire={via_annuaire}, OpenID={via_openid} : le repli "
                   f"OpenID produirait des abonnements muets, ne pas l'utiliser")

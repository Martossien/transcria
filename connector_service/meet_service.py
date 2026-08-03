"""Service Meet — boucle permanente : maintenir les abonnements, ingérer les enregistrements.

POURQUOI UN MODULE ET NON UN SCRIPT. La chaîne Meet a d'abord vécu dans
`scripts/meet_ingest.py`, le temps de l'éprouver contre un vrai Workspace. Un outil de
campagne ne se supervise pas : pas d'unité systemd, pas de redémarrage, et surtout aucune
logique testable — c'est la règle de la maison (« la logique métier descend dans un module
testé, le script délègue »). Tout ce qui décide vit donc ici ; le script et l'unité systemd
ne font plus que construire et lancer.

TROIS COMPORTEMENTS QUE CE SERVICE DOIT AVOIR, et qu'un simple `while True` n'a pas :

1. **Démarrer même mal configuré.** Une unité systemd qui plante au démarrage parce que la
   fiche Meet est vide entre en boucle de redémarrage et noie le journal. Ce service
   DORT et redit périodiquement ce qui manque — comme le meeting-runner, qui attend le clic
   « Activer » sans rien exiger.
2. **Maintenir AVANT de sonder.** Un abonnement expiré ne délivre plus rien ; découvrir au
   bout d'une heure qu'on interrogeait une file condamnée, c'est une heure perdue.
3. **Ne jamais laisser une panne d'un côté arrêter l'autre.** Le maintien qui échoue ne doit
   pas interrompre l'ingestion, et l'inverse non plus : ce sont deux dépendances distinctes
   (API Workspace Events, API Meet + Drive + portail) qui tombent séparément.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path

from connector_service.bridge import JobsApiBridge
from connector_service.fetchers import GoogleDriveFetcher
from connector_service.meet_api_client import MEET_SETTINGS_SCOPE, participant_names
from connector_service.meet_api_client import MeetApiClient as MeetSpacesClient
from connector_service.meet_calendar import CALENDAR_SCOPE, discover_and_prepare
from connector_service.meet_desired import (
    EnsureOutcome,
    ensure_subscriptions,
    ensure_user_subscriptions,
)
from connector_service.meet_directory import (
    OPENID_SCOPE,
    UserResolver,
    userinfo_call,
)
from connector_service.meet_events import RECORDING_FILE_GENERATED
from connector_service.meet_keeper import MeetSubscriptionKeeper
from connector_service.meet_poller import MeetPoller
from connector_service.meet_report import write_report
from connector_service.oauth import GoogleOAuth
from connector_service.providers.meet import MeetApiClient, MeetArtifactProvider
from connector_service.pubsub_pull import (
    PUBSUB_SCOPE,
    acknowledge_request,
    parse_pull_response,
    pull_request,
)
from connector_service.reconciler import ProviderReconciler
from connector_service.transports import RequestsTransport
from connector_service.workspace_events_client import (
    MEET_SUBSCRIPTION_SCOPE,
    WorkspaceEventsClient,
    default_transport,
)

logger = logging.getLogger(__name__)

# Portées de l'INGESTION : lire la ressource d'enregistrement puis le média. Toutes deux
# déléguées — l'enregistrement est dans le Drive de l'organisateur, pas dans celui du compte
# de service. La portée Pub/Sub, elle, n'est JAMAIS déléguée (cf. `meet_poller`).
INGEST_SCOPES = ("https://www.googleapis.com/auth/meetings.space.readonly",
                 "https://www.googleapis.com/auth/drive.readonly")

# Abonnements à maintenir : ceux qui portent l'évènement déclencheur. Google exige un filtre.
SUBSCRIPTION_FILTER = f'event_types:"{RECORDING_FILE_GENERATED}"'


def _lire_json(methode: str, url: str, jeton: str):
    """Appel GET authentifié → JSON. Lève sur refus, avec le corps en clair."""
    statut, charge = default_transport(methode, url, None, {"Authorization": f"Bearer {jeton}"})
    if statut >= 400:
        raise RuntimeError(f"HTTP {statut} — {charge[:160]}")
    return json.loads(charge or "{}")


def _fusionner(a: EnsureOutcome, b: EnsureOutcome) -> EnsureOutcome:
    """Deux bilans → un seul, sans doublon. Les listes sont des comptes rendus d'affichage :
    les concaténer sans dédoublonner ferait apparaître deux fois une réunion couverte par un
    abonnement utilisateur ET par une salle déclarée."""
    def _unique(x, y):
        vus = list(x)
        return vus + [v for v in y if v not in vus]
    return EnsureOutcome(
        wanted=a.wanted + b.wanted,
        already=_unique(a.already, b.already), created=_unique(a.created, b.created),
        failed=_unique(a.failed, b.failed), extra=_unique(a.extra, b.extra),
        auto_recording=_unique(a.auto_recording, b.auto_recording))


class MeetNotConfigured(RuntimeError):
    """Fiche Meet incomplète — le message nomme la clé et où la renseigner."""


@dataclass(frozen=True)
class MeetServiceConfig:
    """Ce qu'il faut pour tourner. Construit depuis la fiche d'administration."""

    service_account: dict
    impersonate: str
    subscription: str
    portal_url: str
    portal_token: str
    # Réunions à surveiller (liens/codes saisis dans l'interface) et sujet où publier. Le
    # sujet se DÉDUIT du projet de l'abonnement : une seule valeur à saisir pour l'admin.
    wanted_spaces: tuple[str, ...] = ()
    # Utilisateurs surveillés — un abonnement CHACUN, ce qui couvre toutes leurs réunions.
    # C'est le modèle principal ; `wanted_spaces` ne sert plus qu'aux salles particulières
    # (une salle de réunion physique, un canal permanent) que personne n'« organise ».
    watched_users: tuple[str, ...] = ()
    prepare_from_calendar: bool = True
    instance_path: str = "instance"
    poll_timeout_s: float = 60.0
    upload_timeout_s: float = 600.0
    keep_every_s: float = 3600.0
    idle_report_s: float = 900.0        # à quelle fréquence redire « mal configuré »
    heartbeat_s: float = 900.0          # à quelle fréquence dire « je tourne, file vide »

    @staticmethod
    def from_identities(identities: dict[str, str], *, portal_url: str, portal_token: str,
                        **reste) -> MeetServiceConfig:
        """Identités (fiche Meet ou environnement) → configuration validée.

        Les manques sont nommés UN PAR UN : « configuration incomplète » obligerait
        l'exploitant à deviner lequel des trois champs il a oublié.
        """
        def exige(cle: str) -> str:
            valeur = str(identities.get(cle) or "").strip()
            if not valeur:
                raise MeetNotConfigured(
                    f"{cle} absent — Administration → Connecteurs → fiche Meet")
            return valeur

        brut = exige("MEET_SERVICE_ACCOUNT_JSON")
        try:
            compte = json.loads(brut if brut.lstrip().startswith("{")
                                else Path(brut).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MeetNotConfigured(
                f"clé de compte de service illisible ({exc.__class__.__name__}) — "
                f"la redéposer depuis la fiche Meet") from exc
        if not portal_token:
            raise MeetNotConfigured(
                "jeton d'exécutant absent — activer les réunions dans "
                "Administration → Connecteurs (le bouton le dépose)")
        return MeetServiceConfig(service_account=compte, impersonate=exige("MEET_IMPERSONATE_USER"),
                                 subscription=exige("MEET_PUBSUB_SUBSCRIPTION"),
                                 portal_url=portal_url, portal_token=portal_token, **reste)

    @property
    def topic(self) -> str:
        """Sujet Pub/Sub, déduit du projet de l'abonnement.

        L'abonnement est pleinement qualifié (`projects/P/subscriptions/S`) : en tirer le
        projet évite une quatrième valeur à saisir, donc une quatrième occasion de se
        tromper. La convention de nom du sujet est celle de notre procédure d'installation.
        """
        projet = self.subscription.split("/")[1]
        return f"projects/{projet}/topics/{projet}-events"


def build_reconciler(config: MeetServiceConfig, *, http_get=None) -> ProviderReconciler:
    """Chaîne d'ingestion : API Meet → Drive → portail.

    Extrait de `build_poller` parce qu'il sert AUSSI seul, pour rejouer une réunion dont
    l'évènement a déjà été acquitté. L'exposer évite qu'un appelant aille chercher l'attribut
    privé du sondeur — un couplage qui casse à la première refonte.
    """
    delegue = GoogleOAuth(service_account_info=config.service_account, scopes=INGEST_SCOPES,
                          subject=config.impersonate)
    return ProviderReconciler(
        MeetArtifactProvider(MeetApiClient(delegue, http_get=http_get)),
        JobsApiBridge(config.portal_url, config.portal_token,
                      RequestsTransport(timeout=config.upload_timeout_s)),
        fetch_audio=GoogleDriveFetcher(delegue).fetch)


def build_poller(config: MeetServiceConfig, *, transport=None, http_get=None) -> MeetPoller:
    """Sondeur prêt à tourner. `transport`/`http_get` injectés par les tests."""
    # SANS `subject` : la file appartient au projet Cloud, le compte de service l'interroge
    # en son propre nom (droit Cloud IAM, pas délégation Workspace).
    pubsub = GoogleOAuth(service_account_info=config.service_account, scopes=(PUBSUB_SCOPE,))
    reconciler = build_reconciler(config, http_get=http_get)

    appel = _pubsub_caller(pubsub, config.poll_timeout_s, transport)

    async def pull():
        url, corps = pull_request(config.subscription)
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: parse_pull_response(appel(url, corps)))

    async def acknowledge(ack_ids):
        url, corps = acknowledge_request(config.subscription, ack_ids)
        await asyncio.get_event_loop().run_in_executor(None, lambda: appel(url, corps))

    def participants_of(conference_record):
        salles = MeetSpacesClient(GoogleOAuth(
            service_account_info=config.service_account, scopes=INGEST_SCOPES,
            subject=config.impersonate).token)
        noms = participant_names(salles.participants(conference_record))
        return {"names": noms, "count": len(noms)} if noms else None

    return MeetPoller(pull=pull, acknowledge=acknowledge, reconciler=reconciler,
                      organizer=config.impersonate, participants_of=participants_of)


def build_keeper(config: MeetServiceConfig, *, transport=None) -> MeetSubscriptionKeeper:
    """Gardien des abonnements. Portée d'ABONNEMENT, distincte de celles de l'ingestion :
    renouveler n'est pas lire un enregistrement, et mélanger les portées ferait demander à
    l'administrateur du domaine plus de droits que nécessaire."""
    oauth = GoogleOAuth(service_account_info=config.service_account,
                        scopes=(MEET_SUBSCRIPTION_SCOPE,), subject=config.impersonate)
    client = WorkspaceEventsClient(oauth.token, transport or default_transport)
    return MeetSubscriptionKeeper(client, filtre=SUBSCRIPTION_FILTER)


def _pubsub_caller(oauth, timeout: float, transport=None):
    """Appel Pub/Sub REST. Le jeton est redemandé à CHAQUE appel : `GoogleOAuth` le met en
    cache et le rafraîchit seul, alors qu'un jeton capturé une fois au démarrage expirerait
    dans un service qui, lui, tourne des semaines."""
    def appel(url: str, corps: dict) -> str:
        if transport is not None:
            return transport(url, corps)
        import urllib.error
        import urllib.request

        requete = urllib.request.Request(
            url, data=json.dumps(corps).encode(),
            headers={"Authorization": f"Bearer {oauth.token()}",
                     "Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(requete, timeout=timeout) as reponse:
                return reponse.read().decode()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Pub/Sub HTTP {exc.code} — {exc.read().decode()[:200]}") from exc
    return appel


class MeetService:
    """Boucle permanente. `build` est injectable : les tests n'ouvrent aucune connexion."""

    def __init__(self, load_config, *, build=None, sleep=None) -> None:
        self._load_config = load_config
        self._build = build or (lambda cfg: (build_poller(cfg), build_keeper(cfg)))
        self._sleep = sleep or asyncio.sleep
        self._running = False
        self._prochain_maintien = 0.0
        self._prochain_battement = 0.0
        self._tours = 0
        self._souhaits = EnsureOutcome()
        # Les DERNIERS jobs créés, à travers les tours. Ne rapporter que ceux du tour courant
        # rendait le panneau d'administration muet en pratique : un job est créé en une
        # seconde, et les centaines de tours suivants publiaient une liste vide.
        self._derniers_jobs: list[str] = []
        # Sondeur et gardien CONSTRUITS UNE FOIS, reconstruits seulement si la configuration
        # change. Les refaire à chaque tour perdait la mémoire locale des ingestions déjà
        # faites (`MeetPoller._seen`) : un évènement redélivré était re-téléchargé de Drive
        # et re-téléversé au portail, pour finir en doublon rejeté par l'idempotence serveur.
        # Correct, mais un enregistrement complet transféré deux fois pour rien.
        self._construits: tuple | None = None
        self._resolveur_cache: UserResolver | None = None

    def stop(self) -> None:
        self._running = False

    async def run_forever(self, *, max_cycles: int = 0) -> int:
        """Tourne jusqu'à `stop()`. `max_cycles` borne les tests (0 = sans fin).

        Une configuration incomplète n'est PAS une erreur fatale : le service dort et le
        redit périodiquement. Une unité systemd qui plante au démarrage entrerait en boucle
        de redémarrage et noierait le journal — sans rien réparer.
        """
        self._running = True
        while self._running and (not max_cycles or self._tours < max_cycles):
            self._tours += 1
            try:
                config = self._load_config()
            except MeetNotConfigured as exc:
                logger.info("[meet] en veille — %s", exc)
                await self._sleep(_IDLE_REPORT_S)
                continue
            if self._construits is None or self._construits[0] != config:
                self._construits = (config, *self._build(config))
                logger.info("[meet] sondeur (re)construit — configuration lue")
            await self._un_tour(config, self._construits[1], self._construits[2])
        return self._tours

    async def _un_tour(self, config, poller, keeper) -> None:
        boucle = asyncio.get_event_loop()
        if boucle.time() >= self._prochain_maintien:
            # AVANT le sondage : un abonnement expiré ne délivre plus rien.
            await self._maintenir(keeper)
            self._souhaits = await boucle.run_in_executor(None, self._reconcilier, config)
            self._prochain_maintien = boucle.time() + config.keep_every_s
        try:
            resultat = await poller.poll_once()
        except Exception:  # noqa: BLE001 — le service ne meurt pas d'un tour raté
            logger.exception("[meet] sondage en échec — nouvelle tentative au prochain tour")
            await self._sleep(min(config.poll_timeout_s, 30.0))
            return
        if resultat.pulled:
            logger.info("[meet] %d message(s), %d déclenchant(s), %d acquitté(s), jobs=%s",
                        resultat.pulled, resultat.triggering, len(resultat.acknowledged),
                        resultat.jobs or "aucun")
            self._prochain_battement = boucle.time() + config.heartbeat_s
        elif boucle.time() >= self._prochain_battement:
            # BATTEMENT DE CŒUR. Une file calme est le cas NORMAL : sans ce rappel, le
            # journal d'un service en bonne santé est rigoureusement identique à celui d'un
            # service figé sur une interrogation qui ne revient jamais.
            logger.info("[meet] en veille active — file vide, %d tour(s) depuis le démarrage",
                        self._tours)
            self._prochain_battement = boucle.time() + config.heartbeat_s
        if resultat.failed:
            logger.warning("[meet] %d ingestion(s) en échec — non acquittées, réessayées",
                           resultat.failed)
        if resultat.jobs:
            self._derniers_jobs = (resultat.jobs + self._derniers_jobs)[:DERNIERS_JOBS_AFFICHES]
        self._publier_etat(config, resultat)

    def _reconcilier(self, config) -> EnsureOutcome:
        """Aligner l'état réel sur ce qui est voulu : abonnements et salles à préparer.

        TROIS CHOSES, dans cet ordre, et l'ordre compte :

        1. **un abonnement par UTILISATEUR** — c'est ce qui couvre toutes ses réunions ;
        2. **les salles particulières** de la liste d'administration, pour ce que personne
           n'organise (salle physique, canal permanent) ;
        3. **le pré-réglage depuis l'AGENDA** — les réunions à venir sont réglées pour
           s'enregistrer seules.

        Préparer une salle sans s'abonner à son propriétaire produirait une réunion
        parfaitement enregistrée dont personne n'est jamais prévenu : l'abonnement passe donc
        AVANT (vécu le 2026-08-01, à quelques minutes près).

        Fait au rythme du MAINTIEN, pas à chaque sondage : ces listes ne changent que quand
        un humain les modifie ou qu'un agenda bouge.
        """
        if not (config.wanted_spaces or config.watched_users):
            return EnsureOutcome()
        # DEUX jetons, délibérément. La portée de RÉGLAGE n'est pas accordée par défaut, et
        # Google refuse en bloc un jeton dont une portée manque : les fondre ferait échouer
        # la surveillance elle-même chez un administrateur qui ne l'a pas encore ajoutée.
        delegue = GoogleOAuth(service_account_info=config.service_account,
                              scopes=(MEET_SUBSCRIPTION_SCOPE,), subject=config.impersonate)
        reglages = GoogleOAuth(service_account_info=config.service_account,
                               scopes=(MEET_SUBSCRIPTION_SCOPE, MEET_SETTINGS_SCOPE),
                               subject=config.impersonate)
        evenements = WorkspaceEventsClient(delegue.token)
        try:
            bilan = EnsureOutcome()
            if config.watched_users:
                bilan = ensure_user_subscriptions(
                    users=list(config.watched_users), topic=config.topic,
                    events_client=evenements,
                    resolve_user=self._resolveur(config).resolve,
                    subscriptions_filter=SUBSCRIPTION_FILTER)
            if config.wanted_spaces:
                salles = ensure_subscriptions(
                    wanted=list(config.wanted_spaces), topic=config.topic,
                    events_client=evenements,
                    meet_client=MeetSpacesClient(delegue.token),
                    settings_client=MeetSpacesClient(reglages.token),
                    subscriptions_filter=SUBSCRIPTION_FILTER)
                bilan = _fusionner(bilan, salles)
            if config.prepare_from_calendar and config.watched_users:
                bilan = _fusionner(bilan, self._preparer_agendas(config, reglages))
        except Exception:  # noqa: BLE001 — l'ingestion continue même si l'alignement rate
            logger.exception("[meet] alignement des abonnements en échec")
            return EnsureOutcome(wanted=len(config.wanted_spaces) + len(config.watched_users),
                                 failed=["alignement impossible (voir les journaux)"])
        if bilan.created:
            logger.info("[meet] %d abonnement(s) créé(s) depuis l'interface : %s",
                        len(bilan.created), ", ".join(bilan.created))
        if bilan.failed:
            logger.error("[meet] réunions NON surveillées : %s", "; ".join(bilan.failed))
        return bilan

    def _resolveur(self, config) -> UserResolver:
        """Adresse → identifiant, avec CACHE conservé entre les tours.

        Le recréer à chaque tour reviendrait à ré-authentifier chaque utilisateur toutes les
        heures pour une donnée qui ne change jamais.
        """
        if self._resolveur_cache is None:
            def via_openid(email):
                jeton = GoogleOAuth(service_account_info=config.service_account,
                                    scopes=(OPENID_SCOPE,), subject=email).token()
                return _lire_json(*userinfo_call()[:2], jeton)
            self._resolveur_cache = UserResolver(openid=via_openid)
        return self._resolveur_cache

    def _preparer_agendas(self, config, reglages) -> EnsureOutcome:
        """Réunions À VENIR de chaque utilisateur → salles réglées en auto-enregistrement."""
        from datetime import datetime

        def agenda(adresse, methode, url):
            jeton = GoogleOAuth(service_account_info=config.service_account,
                                scopes=(CALENDAR_SCOPE,), subject=adresse).token()
            return _lire_json(methode, url, jeton)

        bilan = discover_and_prepare(users=list(config.watched_users),
                                     now=datetime.now(UTC),
                                     calendar_call=agenda,
                                     settings_client=MeetSpacesClient(reglages.token))
        return EnsureOutcome(auto_recording=bilan["prepared"], failed=bilan["failed"])

    def _publier_etat(self, config, resultat) -> None:
        """Écrit l'état lu par la page d'administration. Jamais bloquant : ne pas pouvoir
        écrire un compte rendu ne doit pas arrêter l'ingestion."""
        souhaits = self._souhaits
        try:
            write_report(
                config.instance_path,
                cycles=self._tours,
                watched=sorted(set(souhaits.already) | set(souhaits.created)),
                auto_recording=list(souhaits.auto_recording),
                watched_users=list(config.watched_users),
                pending=[libelle.split(" : ")[0] for libelle in souhaits.failed],
                problems=list(souhaits.failed),
                last_jobs=list(self._derniers_jobs),
                subscriptions=[{"target": cible} for cible in souhaits.extra])
        except Exception:  # noqa: BLE001
            logger.warning("[meet] compte rendu d'état non écrit (page admin en retard)")

    async def _maintenir(self, keeper) -> None:
        try:
            bilan = await asyncio.get_event_loop().run_in_executor(None, keeper.keep_once)
        except Exception:  # noqa: BLE001 — l'ingestion continue avec un abonnement en sursis
            logger.exception("[meet] maintien des abonnements en échec — ingestion poursuivie")
            return
        if bilan.renewed or bilan.reactivated:
            logger.info("[meet] abonnements : %d renouvelé(s), %d réactivé(s)",
                        len(bilan.renewed), len(bilan.reactivated))
        if bilan.needs_attention:
            logger.error("[meet] ABONNEMENTS À REGARDER — expirés : %s ; échecs : %s",
                         bilan.to_recreate or "aucun", bilan.failed or "aucun")


#: Combien de comptes rendus récents la page d'administration montre. Assez pour vérifier
#: qu'« il s'est passé quelque chose », pas assez pour devenir un journal.
DERNIERS_JOBS_AFFICHES = 5

#: Attente entre deux rappels « fiche Meet incomplète ». Assez long pour ne pas noyer le
#: journal d'un serveur où Meet n'est simplement pas utilisé.
_IDLE_REPORT_S = 900.0

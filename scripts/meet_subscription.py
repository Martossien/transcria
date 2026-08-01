#!/usr/bin/env python3
"""Abonnements Google Workspace Events pour Meet — créer, inventorier, supprimer.

À QUOI ÇA SERT. Une réunion Meet enregistrée ne produit un évènement que si un ABONNEMENT
relie son espace au sujet Pub/Sub. C'est la pièce sans laquelle tout le reste — file, rôles
IAM, clé de compte de service — reste muet : rien n'est jamais publié, et aucune erreur ne le
signale. Ce script est la façon d'en poser un.

Les identités viennent de la fiche Meet de `/admin/connecteurs` (`platform_env`), avec repli
sur l'environnement — mêmes clés, même ordre de priorité que le portail, pour qu'il n'y ait
jamais deux configurations à tenir.

    python scripts/meet_subscription.py list
    python scripts/meet_subscription.py create https://meet.google.com/abc-mnop-xyz --dry-run
    python scripts/meet_subscription.py create abc-mnop-xyz
    python scripts/meet_subscription.py delete subscriptions/XXXX

⚠ Un abonnement dure SEPT JOURS au maximum. Celui posé ici n'est pas renouvelé
automatiquement tant que le sondeur n'est pas branché (`subscription_keeper` sait le faire,
personne ne l'appelle encore) : pour une campagne d'essais, c'est sans conséquence ; pour un
usage réel, ce serait une panne muette de plus.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE))

from connector_service.meet_api_client import MeetApiClient  # noqa: E402
from connector_service.meet_events import (  # noqa: E402
    DEFAULT_EVENT_TYPES,
    build_subscription_request,
    space_target,
)
from connector_service.meet_keeper import MeetSubscriptionKeeper  # noqa: E402
from connector_service.meet_main import read_identities  # noqa: E402
from connector_service.oauth import GoogleOAuth  # noqa: E402
from connector_service.pubsub_pull import (  # noqa: E402
    PUBSUB_SCOPE,
    acknowledge_request,
    parse_pull_response,
    pull_request,
    to_meet_event,
)
from connector_service.workspace_events_client import (  # noqa: E402
    MEET_SUBSCRIPTION_SCOPE,
    WorkspaceEventsClient,
    WorkspaceEventsError,
)


class ConfigurationManquante(RuntimeError):
    """Identité absente — le message dit où la renseigner."""


def _identite(cle: str) -> str:
    """Identité de la fiche Meet, environnement en repli.

    La lecture est CELLE DU SERVICE (`meet_main.read_identities`) : deux lectures
    différentes finiraient par diverger, et l'écart ne se verrait qu'au moment où l'outil
    d'administration et le service ne parlent plus de la même configuration.
    """
    valeur = str(read_identities(RACINE).get(cle) or "").strip()
    if not valeur:
        raise ConfigurationManquante(
            f"{cle} absent — le renseigner dans Administration → Connecteurs → fiche Meet")
    return valeur


def _clients() -> tuple[WorkspaceEventsClient, MeetApiClient, str]:
    """(client abonnements, client Meet, sujet Pub/Sub). Jeton DÉLÉGUÉ dans les deux cas :
    espaces et abonnements sont des données d'utilisateur."""
    brut = _identite("MEET_SERVICE_ACCOUNT_JSON")
    info = json.loads(brut if brut.lstrip().startswith("{")
                      else Path(brut).read_text(encoding="utf-8"))
    utilisateur = _identite("MEET_IMPERSONATE_USER")
    oauth = GoogleOAuth(service_account_info=info, scopes=(MEET_SUBSCRIPTION_SCOPE,),
                        subject=utilisateur)
    # Le sujet se déduit de l'abonnement : même projet, et une seule valeur à saisir.
    abonnement = _identite("MEET_PUBSUB_SUBSCRIPTION")
    projet = abonnement.split("/")[1]
    sujet = os.environ.get("MEET_PUBSUB_TOPIC") or f"projects/{projet}/topics/{projet}-events"
    return (WorkspaceEventsClient(oauth.token), MeetApiClient(oauth.token), sujet)


# Filtre par défaut de l'inventaire : l'évènement qui DÉCLENCHE une ingestion. Google exige
# un filtre (`event_types` ou `target_resource`) et rejette une demande sans, sans le dire.
FILTRE_DEFAUT = 'event_types:"google.workspace.meet.recording.v2.fileGenerated"'


def cmd_list(args) -> int:
    abonnements, _, _ = _clients()
    trouves = abonnements.list(getattr(args, "filter", "") or FILTRE_DEFAUT)
    if not trouves:
        print("aucun abonnement — aucune réunion ne produira d'évènement")
        return 0
    for a in trouves:
        print(f"{a.get('name')}  cible={a.get('targetResource')}  état={a.get('state')}  "
              f"expire={a.get('expireTime')}")
    return 0


def cmd_create(args) -> int:
    abonnements, meet, sujet = _clients()
    sujet = args.topic or sujet
    espace = meet.resolve_space(args.meeting)
    print(f"espace résolu : {espace}   (depuis « {args.meeting} »)")
    corps = build_subscription_request(target_resource=space_target(espace),
                                       pubsub_topic=sujet,
                                       event_types=tuple(args.events or DEFAULT_EVENT_TYPES))
    print(f"sujet visé    : {sujet}")
    print(f"évènements    : {', '.join(corps['eventTypes'])}")
    resultat = abonnements.create(corps, validate_only=args.dry_run)
    if args.dry_run:
        print("VALIDÉ par Google — rien n'a été créé (--dry-run)")
    else:
        print(f"CRÉÉ : {resultat.get('name')}  expire={resultat.get('expireTime')}")
    return 0


def cmd_keep(args) -> int:
    """Un tour de MAINTIEN EN VIE — renouvelle ce qui approche de l'échéance.

    Un abonnement Workspace Events vit sept jours, et Google SUPPRIME définitivement celui
    qui expire : ni renouvelable, ni réactivable. Le silence qui suit ressemble alors à
    « aucune réunion n'a été enregistrée », une semaine après que tout marchait.
    """
    abonnements, _, _ = _clients()
    resultat = MeetSubscriptionKeeper(abonnements, filtre=args.filter or FILTRE_DEFAUT).keep_once()
    print(f"{resultat.inspected} abonnement(s) inspecté(s)")
    for libelle, valeurs in (("renouvelé", resultat.renewed), ("réactivé", resultat.reactivated),
                             ("À RECRÉER (expiré)", resultat.to_recreate),
                             ("ÉCHEC", resultat.failed), ("reporté", resultat.skipped)):
        for valeur in valeurs:
            print(f"  {libelle} : {valeur}")
    if not any((resultat.renewed, resultat.reactivated, resultat.to_recreate,
                resultat.failed, resultat.skipped)):
        print("  rien à faire — aucune échéance proche")
    return 1 if resultat.needs_attention else 0


def cmd_pull(args) -> int:
    """Interroge la file et affiche ce qui arrive — l'observatoire des essais réels.

    Jeton du COMPTE DE SERVICE SEUL (pas d'impersonation) : la file appartient au projet,
    pas à un utilisateur. Acquittement OPT-IN (`--ack`) : pendant une campagne d'essais, on
    veut pouvoir relire le même évènement plutôt que de le consommer du premier coup.
    """
    import urllib.error
    import urllib.request

    brut = _identite("MEET_SERVICE_ACCOUNT_JSON")
    info = json.loads(brut if brut.lstrip().startswith("{")
                      else Path(brut).read_text(encoding="utf-8"))
    jeton = GoogleOAuth(service_account_info=info, scopes=(PUBSUB_SCOPE,)).token()
    abonnement = _identite("MEET_PUBSUB_SUBSCRIPTION")

    def appel(url, corps):
        requete = urllib.request.Request(
            url, data=json.dumps(corps).encode(),
            headers={"Authorization": f"Bearer {jeton}", "Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(requete, timeout=args.timeout) as reponse:
                return reponse.read().decode()
        except urllib.error.HTTPError as exc:
            raise WorkspaceEventsError(
                f"HTTP {exc.code} — {exc.read().decode()[:200]}") from exc

    url, corps = pull_request(abonnement, max_messages=args.max)
    messages = parse_pull_response(appel(url, corps))
    if not messages:
        print("file vide — aucun évènement pour l'instant")
        return 0
    a_acquitter = []
    for message in messages:
        evenement = to_meet_event(message)
        if evenement is None:
            print(f"[{message.message_id}] message ILLISIBLE (sera acquitté : il ne le "
                  f"deviendra jamais)")
        else:
            print(f"[{message.publish_time}] {evenement.event_type}")
            print(f"    ressource  : {evenement.resource_name}")
            print(f"    réunion    : {evenement.conference_record}")
            if evenement.is_recording_ready:
                print("    → ENREGISTREMENT PRÊT : c'est l'évènement qui déclenche l'ingestion")
        a_acquitter.append(message.ack_id)
    if args.ack:
        url, corps = acknowledge_request(abonnement, tuple(a_acquitter))
        appel(url, corps)
        print(f"{len(a_acquitter)} message(s) acquitté(s)")
    else:
        print(f"{len(messages)} message(s) NON acquittés — relisibles (ajouter --ack pour "
              f"les consommer)")
    return 0


def cmd_delete(args) -> int:
    abonnements, _, _ = _clients()
    abonnements.delete(args.name)
    print(f"supprimé : {args.name}")
    return 0


def main(argv=None) -> int:
    parseur = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sous = parseur.add_subparsers(dest="commande", required=True)
    inventaire = sous.add_parser("list", help="inventorier les abonnements existants")
    inventaire.add_argument("--filter", default="",
                            help="filtre API (défaut : les abonnements « enregistrement prêt »)")
    inventaire.set_defaults(fn=cmd_list)
    creer = sous.add_parser("create", help="abonner un espace Meet au sujet Pub/Sub")
    creer.add_argument("meeting", help="lien de réunion, code (abc-mnop-xyz) ou spaces/…")
    creer.add_argument("--topic", default="", help="sujet Pub/Sub (défaut : déduit du projet)")
    creer.add_argument("--events", nargs="*", help="types d'évènements (défaut : fin + fichier)")
    creer.add_argument("--dry-run", action="store_true",
                       help="valider la demande SANS créer (validateOnly)")
    creer.set_defaults(fn=cmd_create)
    maintien = sous.add_parser("keep", help="renouveler les abonnements dont l'échéance approche")
    maintien.add_argument("--filter", default="", help="filtre API (défaut : « enregistrement prêt »)")
    maintien.set_defaults(fn=cmd_keep)
    sonder = sous.add_parser("pull", help="interroger la file Pub/Sub et afficher les évènements")
    sonder.add_argument("--ack", action="store_true", help="acquitter (défaut : relisibles)")
    sonder.add_argument("--max", type=int, default=10, help="messages au plus (défaut 10)")
    sonder.add_argument("--timeout", type=float, default=60.0,
                        help="attente en secondes (une file vide fait patienter)")
    sonder.set_defaults(fn=cmd_pull)
    supprimer = sous.add_parser("delete", help="supprimer un abonnement")
    supprimer.add_argument("name", help="subscriptions/…")
    supprimer.set_defaults(fn=cmd_delete)
    args = parseur.parse_args(argv)
    try:
        return int(args.fn(args))
    except (WorkspaceEventsError, ConfigurationManquante) as exc:
        print(f"ÉCHEC : {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

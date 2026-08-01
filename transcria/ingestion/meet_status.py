"""Compte rendu du service Meet — le canal de DONNÉE qui le rend visible dans l'interface.

POURQUOI CE FICHIER PLUTÔT QU'UN APPEL. Le portail ne peut pas interroger le service Meet :
le cœur `transcria/` n'importe jamais `connector_service` (contrat d'imports), et le service
tourne dans un autre processus. Il écrit donc son état dans un petit fichier JSON que la page
d'administration lit — même doctrine que le catalogue de connecteurs.

CE QUE LA PAGE DOIT POUVOIR DIRE, et qu'un simple « actif/inactif » ne dirait pas :

- le service tourne-t-il ENCORE ? Un fichier d'il y a deux jours signale un service arrêté
  aussi sûrement qu'une absence de fichier, et bien mieux qu'un état figé sur « OK » ;
- les réunions demandées sont-elles réellement SURVEILLÉES ? C'est la question qui compte :
  un abonnement manquant ne produit aucune erreur, seulement un compte rendu qui n'arrive
  jamais.

Module PUR : lecture, écriture et jugement de fraîcheur, sans Flask ni réseau.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATUS_FILENAME = "meet_status.json"

#: Au-delà, l'état affiché n'est plus digne de confiance. Le service écrit à chaque tour
#: (quelques dizaines de secondes) : trois minutes laissent passer un tour lent sans crier
#: au loup, et signalent un service arrêté bien avant qu'on cherche un compte rendu absent.
STALE_AFTER = timedelta(minutes=3)


@dataclass
class MeetStatus:
    """État du service Meet, tel qu'il s'écrit et se relit."""

    updated_at: str = ""
    cycles: int = 0
    watched: list[str] = field(default_factory=list)     # réunions effectivement surveillées
    pending: list[str] = field(default_factory=list)     # demandées, PAS encore surveillées
    problems: list[str] = field(default_factory=list)
    last_jobs: list[str] = field(default_factory=list)
    subscriptions: list[dict] = field(default_factory=list)
    #: Réunions dont l'espace est réglé pour s'enregistrer SEUL. Une réunion surveillée sans
    #: cela dépend encore d'un humain qui pense à cliquer « Enregistrer ».
    auto_recording: list[str] = field(default_factory=list)
    #: Personnes dont TOUTES les réunions sont couvertes (un abonnement chacune).
    watched_users: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return not self.problems and not self.pending


def status_path(instance_path: str | Path) -> Path:
    return Path(instance_path) / STATUS_FILENAME


def write_status(instance_path: str | Path, status: MeetStatus, *,
                 now: datetime | None = None) -> Path:
    """Écrit l'état. L'horodatage est POSÉ ICI : un état sans date ne permettrait pas de
    distinguer un service vivant d'un service arrêté depuis deux jours."""
    status.updated_at = (now or datetime.now(timezone.utc)).astimezone(
        timezone.utc).isoformat()
    cible = status_path(instance_path)
    cible.parent.mkdir(parents=True, exist_ok=True)
    # Écriture ATOMIQUE : la page peut lire à tout instant, et un JSON tronqué s'afficherait
    # comme une panne du service alors qu'il se porte bien.
    provisoire = cible.with_suffix(".tmp")
    provisoire.write_text(json.dumps(asdict(status), ensure_ascii=False, indent=2),
                          encoding="utf-8")
    provisoire.replace(cible)
    return cible


def read_status(instance_path: str | Path) -> MeetStatus | None:
    """État écrit par le service, ou `None` s'il n'a jamais tourné (ni fichier, ni illisible).

    Un fichier illisible est traité comme une absence : afficher un état partiel serait pire
    que d'afficher « jamais démarré », qui au moins oriente vers la bonne question.
    """
    try:
        donnees = json.loads(status_path(instance_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(donnees, dict):
        return None
    connus = {champ for champ in MeetStatus().__dict__}
    return MeetStatus(**{k: v for k, v in donnees.items() if k in connus})


def is_stale(status: MeetStatus, *, now: datetime | None = None) -> bool:
    """L'état est-il périmé ? Un horodatage illisible compte comme périmé — on ne présente
    jamais comme vivant un service dont on ne sait rien."""
    if not status.updated_at:
        return True
    try:
        ecrit = datetime.fromisoformat(status.updated_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    maintenant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return maintenant - ecrit.astimezone(timezone.utc) > STALE_AFTER

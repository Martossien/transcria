"""Catalogue des connecteurs de réunion — lecture et validation du fichier de données.

POURQUOI CE MODULE EXISTE : la page d'administration doit décrire ce que chaque connecteur
exige, alors que le cœur applicatif n'a PAS le droit d'importer `connector_service` (contrat
d'imports « Le cœur n'importe pas le service connecteur async »). Le catalogue est donc de la
DONNÉE (`transcria/data/meeting_connectors.yaml`), lue ici — même motif que
`meeting_types.yaml`, et même bénéfice : la description évolue sans toucher au code.

La validation est FAIL-LOUD à la lecture : un catalogue mal formé doit se voir au démarrage,
pas produire une page d'administration silencieusement incomplète.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CATALOG_PATH = Path(__file__).resolve().parent.parent / "data" / "meeting_connectors.yaml"

# Ce que dit `status` — la distinction est le cœur de l'honnêteté de la page :
#   validated    éprouvé en conditions réelles (réunion ou serveur réel)
#   implemented  le code existe et passe la CI, mais n'a JAMAIS été exécuté en vrai
#   planned      rien d'exploitable
VALID_STATUSES = ("validated", "implemented", "planned")

# Comment les évènements ou l'audio nous parviennent — ce qui décide de l'exigence RÉSEAU :
#   bot      un participant rejoint la réunion (sortant seulement)
#   native   transport natif de la plateforme (sortant seulement)
#   webhook  la plateforme APPELLE une URL publique (ouverture de pare-feu)
#   pull     la plateforme dépose dans une file que l'on interroge (sortant seulement)
# `pull` n'est pas un détail d'implémentation : c'est la voie Google Meet, et son absence de
# port entrant est précisément ce qui la rend acceptable là où un webhook est refusé.
VALID_PATHS = ("bot", "native", "webhook", "pull")


# Comment l'administrateur FOURNIT le renseignement :
#   text       une chaîne saisie dans le formulaire
#   json_file  un FICHIER JSON téléversé (clé de compte de service…) — déposé en 0600 hors
#              configuration, c'est son CHEMIN qui est stocké. Coller le contenu d'une clé
#              privée dans `config.yaml` reste possible mais n'est plus la voie proposée.
VALID_FIELD_KINDS = ("text", "json_file")


@dataclass(frozen=True)
class RequiredField:
    """Un renseignement à fournir pour activer un connecteur."""

    key: str
    label: str
    secret: bool = False
    kind: str = "text"
    # Clés que le fichier téléversé doit porter — ce qui permet de refuser à l'arrivée le
    # mauvais JSON plutôt qu'à la première authentification. Vide hors `json_file`.
    expects: tuple[str, ...] = ()


@dataclass(frozen=True)
class Connector:
    """Un connecteur tel que la page d'administration doit le présenter."""

    id: str
    name: str
    path: str
    status: str
    summary: str
    doc: str = ""
    verified_on: str = ""
    requires: tuple[RequiredField, ...] = ()
    steps: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    testable: bool = False

    @property
    def is_ready(self) -> bool:
        """Éprouvé en conditions réelles. Le reste ne doit pas être présenté comme prêt."""
        return self.status == "validated"

    @property
    def needs_inbound_port(self) -> bool:
        """Exige-t-il une ouverture de pare-feu ?

        Détail structurant en entreprise : un connecteur `webhook` reçoit un appel ENTRANT de
        la plateforme, là où bots, transports natifs et files interrogées (`pull`) n'établissent
        que du sortant. C'est souvent ce qui décide de ce qu'une DSI acceptera de déployer.
        """
        return self.path == "webhook"


class CatalogError(ValueError):
    """Catalogue de connecteurs illisible ou incohérent."""


def _field(raw: Any, index: int, connector_id: str) -> RequiredField:
    if not isinstance(raw, dict) or not raw.get("key"):
        raise CatalogError(f"{connector_id} : champ requis n°{index} sans clé")
    kind = str(raw.get("kind") or "text")
    if kind not in VALID_FIELD_KINDS:
        raise CatalogError(f"{connector_id} : nature de champ inconnue « {kind} » "
                           f"(attendu : {', '.join(VALID_FIELD_KINDS)})")
    return RequiredField(key=str(raw["key"]),
                         label=str(raw.get("label") or raw["key"]),
                         secret=bool(raw.get("secret")),
                         kind=kind,
                         expects=tuple(str(c) for c in (raw.get("expects") or [])))


def _connector(raw: Any, index: int) -> Connector:
    if not isinstance(raw, dict):
        raise CatalogError(f"connecteur n°{index} : objet attendu")
    for obligatoire in ("id", "name", "path", "status", "summary"):
        if not raw.get(obligatoire):
            raise CatalogError(f"connecteur n°{index} : « {obligatoire} » manquant")
    if raw["status"] not in VALID_STATUSES:
        raise CatalogError(f"{raw['id']} : statut inconnu « {raw['status']} » "
                           f"(attendu : {', '.join(VALID_STATUSES)})")
    if raw["path"] not in VALID_PATHS:
        raise CatalogError(f"{raw['id']} : voie inconnue « {raw['path']} » "
                           f"(attendu : {', '.join(VALID_PATHS)})")
    # Un connecteur déclaré éprouvé DOIT dire quand : sans date, l'affirmation ne vaut rien
    # et se périme sans qu'on s'en aperçoive.
    if raw["status"] == "validated" and not raw.get("verified_on"):
        raise CatalogError(f"{raw['id']} : « validated » exige « verified_on »")
    return Connector(
        id=str(raw["id"]),
        name=str(raw["name"]),
        path=str(raw["path"]),
        status=str(raw["status"]),
        summary=str(raw["summary"]).strip(),
        doc=str(raw.get("doc") or ""),
        verified_on=str(raw.get("verified_on") or ""),
        requires=tuple(_field(f, i, str(raw["id"]))
                       for i, f in enumerate(raw.get("requires") or [], 1)),
        steps=tuple(str(s).strip() for s in (raw.get("steps") or [])),
        notes=tuple(str(n).strip() for n in (raw.get("notes") or [])),
        testable=bool(raw.get("testable")),
    )


def parse_catalog(data: Any) -> list[Connector]:
    """Structure YAML → connecteurs validés. Lève `CatalogError` au moindre défaut."""
    if not isinstance(data, dict) or not isinstance(data.get("connectors"), list):
        raise CatalogError("catalogue invalide : clé « connectors » (liste) attendue")
    connectors = [_connector(raw, i) for i, raw in enumerate(data["connectors"], 1)]
    identifiants = [c.id for c in connectors]
    doublons = {i for i in identifiants if identifiants.count(i) > 1}
    if doublons:
        raise CatalogError(f"identifiants en double : {', '.join(sorted(doublons))}")
    return connectors


def load_catalog(path: Path | None = None) -> list[Connector]:
    """Lit le catalogue depuis le disque."""
    target = path or CATALOG_PATH
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CatalogError(f"catalogue de connecteurs introuvable : {target}") from exc
    return parse_catalog(data)


@dataclass
class ConnectorView:
    """Un connecteur ENRICHI de son état de configuration, pour l'affichage."""

    connector: Connector
    configured: bool
    missing: tuple[str, ...] = field(default_factory=tuple)


def describe_configuration(connector: Connector, provided: dict[str, str]) -> ConnectorView:
    """Croise ce que le connecteur EXIGE avec ce qui est effectivement fourni.

    Une valeur vide vaut absente : une variable déclarée mais laissée vide est le cas le plus
    fréquent, et l'afficher comme « configuré » enverrait chercher la panne ailleurs.
    """
    manquants = tuple(f.label for f in connector.requires
                      if not str(provided.get(f.key) or "").strip())
    return ConnectorView(connector=connector,
                         configured=not manquants,
                         missing=manquants)

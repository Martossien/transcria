"""Compte rendu d'état du service Meet — l'ÉCRIVAIN, côté connecteur.

Le lecteur vit dans `transcria/ingestion/meet_status.py`, et c'est volontaire : le service
connecteur doit pouvoir tourner sur une machine où `transcria` n'est PAS installé (ADR-001 :
« il peut vivre sur une autre machine »). Importer le module du portail pour écrire ce
fichier marcherait ici et casserait là-bas, au pire moment — sur l'exécutant distant, sans
personne pour lire la trace.

LE CONTRAT EST DONC LE FORMAT, pas un module partagé. Les deux côtés sont tenus ensemble par
un test qui fait lire au portail ce que le connecteur écrit : c'est lui qui casse si l'un des
deux dérive, et non un exploitant devant une page vide.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

STATUS_FILENAME = "meet_status.json"


def write_report(instance_path: str | Path, *, cycles: int, watched: list[str],
                 pending: list[str], problems: list[str], last_jobs: list[str],
                 subscriptions: list[dict], auto_recording: list[str] | None = None,
                 watched_users: list[str] | None = None,
                 now: datetime | None = None) -> Path:
    """Écrit l'état lu par la page d'administration.

    L'horodatage est posé ICI : sans lui, la page ne distinguerait pas un service vivant
    d'un service arrêté depuis deux jours — elle afficherait le dernier état connu comme
    s'il était d'aujourd'hui.

    Écriture ATOMIQUE (fichier temporaire puis remplacement) : la page peut lire à tout
    instant, et un JSON tronqué s'afficherait comme une panne du service alors qu'il se
    porte bien.
    """
    charge = {
        "updated_at": (now or datetime.now(UTC)).astimezone(UTC).isoformat(),
        "cycles": cycles,
        "watched": list(watched),
        "pending": list(pending),
        "problems": list(problems),
        "last_jobs": list(last_jobs),
        "subscriptions": list(subscriptions),
        # Réunions qui s'enregistrent SEULES. La page doit le distinguer de « surveillée » :
        # surveillée sans enregistrement automatique, c'est un compte rendu qui dépend
        # encore d'un humain qui pense à cliquer.
        "auto_recording": list(auto_recording or []),
        # Personnes dont TOUTES les réunions sont couvertes. C'est le modèle principal :
        # la liste de salles n'est plus qu'un complément pour ce que personne n'organise.
        "watched_users": list(watched_users or []),
    }
    cible = Path(instance_path) / STATUS_FILENAME
    cible.parent.mkdir(parents=True, exist_ok=True)
    provisoire = cible.with_suffix(".tmp")
    provisoire.write_text(json.dumps(charge, ensure_ascii=False, indent=2), encoding="utf-8")
    provisoire.replace(cible)
    return cible

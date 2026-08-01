#!/usr/bin/env python3
"""Sondeur Meet en ligne de commande — façade sur `connector_service.meet_service`.

EN PRODUCTION, ce script n'est PAS le chemin normal : l'unité systemd
`transcria-meet-poller.service` lance `python -m connector_service.meet_main`, qui tourne en
permanence et se supervise. Ce script reste pour les deux usages où l'on veut la main :

    python scripts/meet_ingest.py --once                       # un tour, pour voir
    python scripts/meet_ingest.py --conference conferenceRecords/…   # rejouer une réunion

Le second sert quand l'évènement a déjà été acquitté, ou pour éprouver l'ingestion sans
dépendre du calendrier de publication de Google. C'est le MÊME chemin qu'en fonctionnement
normal — provider, téléchargement Drive, pont — seule l'origine de l'identifiant change.

Aucune logique ici : elle vit dans `connector_service/meet_service.py`, testée sans réseau.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE))

from connector_service.meet_main import load_config  # noqa: E402
from connector_service.meet_service import (  # noqa: E402
    MeetNotConfigured,
    MeetService,
    build_keeper,
    build_poller,
    build_reconciler,
)

logger = logging.getLogger("meet_ingest")


async def _rejouer(conference: str) -> int:
    """Ingère une conférence NOMMÉE, sans attendre d'évènement."""
    from connector_service.contract import ExternalMeetingOccurrence

    config = load_config(RACINE)
    identifiant = conference.removeprefix("conferenceRecords/")
    issues = await build_reconciler(config).reconcile(ExternalMeetingOccurrence(
        provider="meet", provider_account_id=identifiant, external_occurrence_id=identifiant))
    if not issues:
        print("aucun enregistrement FINALISÉ pour cette conférence")
        return 1
    for issue in issues:
        job = issue.result.job_id if issue.result else None
        deja = " (déjà connu du serveur)" if issue.result and issue.result.idempotent else ""
        print(f"{issue.action} : job={job}{deja}")
    return 0


def main(argv=None) -> int:
    parseur = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parseur.add_argument("--once", action="store_true", help="un seul tour puis sortir")
    parseur.add_argument("--conference", default="",
                         help="rejouer CETTE conférence (conferenceRecords/…)")
    parseur.add_argument("-v", "--verbose", action="store_true")
    args = parseur.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.conference:
            return asyncio.run(_rejouer(args.conference))
        service = MeetService(lambda: load_config(RACINE),
                              build=lambda cfg: (build_poller(cfg), build_keeper(cfg)))
        asyncio.run(service.run_forever(max_cycles=1 if args.once else 0))
        return 0
    except MeetNotConfigured as exc:
        print(f"ÉCHEC : {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

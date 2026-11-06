"""Fiches de réunion — projection COURTE du contexte d'un job, en base (table ``job_cards``).

``context/meeting_context.json`` n'est pas une fiche : il porte toute la synthèse et les termes
suspects (médiane mesurée 38 Ko). La fiche en retient ce qui sert à RETROUVER une réunion et à
suivre ses décisions : titre, type, date, confidentialité, participants, sujet, objectif,
ordre du jour, abrégé, décisions, actions. Jamais de transcription ni de termes suspects.

- ``projection`` : fonctions PURES (contexte + participants → fiche) ;
- ``models`` : la table ;
- ``store`` : ``JobCardStore.refresh`` (idempotent par empreinte), lecture, rattrapage ;
- ``hooks`` : accroche sur les écritures de ``JobFilesystem`` + rattrapage au démarrage.

Plan : ``docs/DISCUTER_REUNIONS.md`` § 2.3 (lot A).
"""

"""« Discuter avec mes réunions » — demandes consommées par le worker (docs/DISCUTER_REUNIONS.md).

- ``models`` : ``ask_requests`` (une question, son état, sa réponse), ``ask_turns`` (mémoire
  courte par utilisateur et par conversation), ``ask_turn_citations`` (quels jobs un tour cite —
  supprimer la réunion supprime les tours qui la citent) ;
- ``store`` : création, claim atomique, écriture du résultat, purge de rétention ;
- ``candidates`` : les réunions qu'une demande a le DROIT de voir (visibilité, confidentiel, cloud) ;
- ``prompting`` : rendu des fiches numérotées, messages de chat, garde des citations (PUR) ;
- ``worker`` : traitement d'une demande sous verrou LLM — JAMAIS appelé depuis le web ;
- ``messages`` : textes fr/en/de/es/it/pt des réponses de service.

Boussole : les permissions avant tout (filtre AVANT la LLM, citations vérifiées APRÈS), la LLM
ne sait que ce qu'on lui donne, jamais de repli silencieux.
"""

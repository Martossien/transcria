"""Présélection PURE des réunions pour « Discuter avec mes réunions » (docs/DISCUTER_REUNIONS.md § 2.4).

Aucune base, aucune LLM, aucun import de l'orchestration : des fiches en entrée, un classement en
sortie. Identique sur PostgreSQL et SQLite (tout se passe en Python, sur quelques centaines de
fiches au plus — mesuré : 104 jobs sur l'installation de dev).
"""

#!/usr/bin/env python3
"""transcria doctor — préflight de diagnostic GPU-free.

Vérifie en quelques secondes, sans GPU et sans effet de bord, les pannes qui
sinon se traduisent par des jobs en échec sans cause lisible : config illisible,
schéma de base dérivé (colonne/table manquante), script de lancement LLM absent
ou non exécutable, LLM d'arbitrage injoignable, binaire opencode manquant, nœud
de ressources distant injoignable, dossiers de travail non inscriptibles.

Usage :
    venv/bin/python scripts/doctor.py [--config config.yaml] [--strict] [--json]

Code de sortie : 0 si aucun échec bloquant (≠ 0 avec --strict si avertissements),
1 sinon. La logique réelle est dans transcria/diagnostics/doctor.py (testée).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from transcria.diagnostics.doctor import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

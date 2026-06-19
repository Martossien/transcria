"""Installateur TranscrIA piloté en Python.

Foyer d'orchestration vers lequel `install.sh` fond progressivement (cf.
`docs/PLAN_EVOLUTION_INSTALLATION.md` §3.2). Chaque phase migrée devient une
fonction Python testée (runner de sous-processus injectable, logique sans effet de
bord vérifiable) invoquée par `python -m transcria.installer.cli <phase>`. Le shell
ne garde que le bootstrap minimal (détection de l'interpréteur, activation du venv)
et la délégation. Le contrat d'assemblage est gardé de bout en bout par
`tests/test_install_e2e.py`.
"""

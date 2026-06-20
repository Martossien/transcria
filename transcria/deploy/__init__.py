"""Outils de déploiement conteneurisé (P5).

Entrypoints Docker par rôle qui réutilisent les invariants des profils
(`web`/`scheduler`/`resource-node`/`migrate`) sans jamais lancer `install.sh` comme
entrypoint applicatif. La logique d'installation reste dans `transcria.installer` ;
ici on ne fait que **provisionner au runtime** (attente DB, garde PostgreSQL) puis
**remplacer le process** par la commande du rôle.
"""

# Changelog

Toutes les évolutions notables de ce dépôt doivent être documentées ici.

Le format suit une logique proche de Keep a Changelog.

## [Unreleased]

### Added
- Worker interne sérialisé pour exécuter les traitements longs hors de la requête HTTP.
- Endpoint `/ready` pour distinguer l’état “process vivant” et “service prêt”.
- Endpoint `/metrics` enrichi avec la capacité et l’activité du worker.
- Script `scripts/bootstrap_config.py` pour générer un `config.yaml` prérempli à partir de l’environnement détecté.
- Workflow GitHub Actions pour exécuter la suite pytest.
- `SECURITY.md` pour documenter le signalement et le périmètre sécurité.

### Changed
- L’API `POST /api/jobs/<id>/process` planifie désormais le traitement en arrière-plan et répond immédiatement.
- Les transitions de workflow liées au pré-traitement et au lancement de traitement sont centralisées.
- Le logging structuré conserve le contexte `job_id` et `step` hors requête Flask.
- La documentation active a été réalignée sur le mode service longue durée.

### Fixed
- Régression de supervision causée par l’absence de `/health` et `/metrics`.
- Gestion plus sûre des relances de jobs bloqués et des annulations.

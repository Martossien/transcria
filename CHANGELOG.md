# Changelog

Toutes les évolutions notables de ce dépôt doivent être documentées ici.

Le format suit une logique proche de Keep a Changelog.

## [Unreleased]

### Added
- Pré-remplissage automatique des rôles participants : la LLM de résumé détecte les rôles SPEAKER_XX et les écrit dans `context/participants.json` après la création du mapping locuteurs (section 5 du wizard pré-remplie).
- Stockage des rôles LLM dans `meeting_context.json["speaker_roles_llm"]` pour persistance inter-phases et réapplication après mapping.
- Récupération des processus opencode orphelins au démarrage du service (`job_executor._kill_orphaned_opencode`) — les jobs interrompus pendant une inférence LLM ne bloquent plus les GPUs.
- Suivi des PID opencode par fichier `.opencode.pid` dans le répertoire du job (jamais de kill aveugle par nom de processus).
- `OpenCodeRunner` utilise `subprocess.Popen` + `_terminate_proc()` (SIGTERM puis SIGKILL) à la place de `subprocess.run()` qui ne tuait pas le processus en cas de timeout.
- Endpoint `POST /api/jobs/<id>/speakers/map` réapplique maintenant les rôles LLM en production après la création du mapping.
- Parser `_parse_structured_summary()` supporte deux formats SPEAKER_XX : `SPEAKER_XX [label] : rôle` (Format A) et `SPEAKER_XX : rôle` (Format B sans label).
- `summary_prompt.txt` v1.3 : chasse systématique aux variantes STT en deux passes, format SPEAKER_XX obligatoire si diarization fournie.
- Test E2E : pyannote lancé en pré-résumé si `diarization_context.md` absent, puis rôles appliqués après l'étape mapping.

### Changed
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

# Changelog

Toutes les évolutions notables de ce dépôt doivent être documentées ici.

Le format suit une logique proche de Keep a Changelog.

## [Unreleased]

### Added
- `correction_prompt.txt` v1.5 : règle `mapped_name` immuable — le modèle recopie le `mapped_name` verbatim (casse, accents, orthographe) sans aucune normalisation ni interprétation. Trois niveaux de défense : définition en Section 1, extraction préalable de la table `speaker_id → mapped_name` avant tout segment (Étape B de la PREMIÈRE ACTION), vérification finale (check 10). Clarification de la sémantique de `replace_by` vide.
- Logs WARNING dans `_parse_structured_summary()` (opencode_runner.py) pour les cas d’échec de parsing : sections `## Participants probables` et `## Termes suspects/douteux` introuvables, champs critiques non extraits (`title_suggere`, `type_suggere`, `sujet_suggere`), termes à zéro malgré section présente.
- Logs WARNING dans `_apply_llm_suggestions()` (runner.py) quand le résumé est indisponible (sentinel) et quand des champs LLM restent vides après parse.
- Logs WARNING dans `run_summary()` (opencode_runner.py) pour les chemins de fallback (`summary.md` absent, repli sur glob `*.md` ou stdout) et le résumé vide après exécution opencode.
- Pré-remplissage automatique des rôles participants : la LLM de résumé détecte les rôles SPEAKER_XX et les écrit dans `context/participants.json` après la création du mapping locuteurs (section 5 du wizard pré-remplie).
- Stockage des rôles LLM dans `meeting_context.json[“speaker_roles_llm”]` pour persistance inter-phases et réapplication après mapping.
- Récupération des processus opencode orphelins au démarrage du service (`job_executor._kill_orphaned_opencode`) — les jobs interrompus pendant une inférence LLM ne bloquent plus les GPUs.
- Suivi des PID opencode par fichier `.opencode.pid` dans le répertoire du job (jamais de kill aveugle par nom de processus).
- `OpenCodeRunner` utilise `subprocess.Popen` + `_terminate_proc()` (SIGTERM puis SIGKILL) à la place de `subprocess.run()` qui ne tuait pas le processus en cas de timeout.
- Endpoint `POST /api/jobs/<id>/speakers/map` réapplique maintenant les rôles LLM en production après la création du mapping.
- Parser `_parse_structured_summary()` supporte deux formats SPEAKER_XX : `SPEAKER_XX [label] : rôle` (Format A) et `SPEAKER_XX : rôle` (Format B sans label).
- `summary_prompt.txt` v1.3 : chasse systématique aux variantes STT en deux passes, format SPEAKER_XX obligatoire si diarization fournie.
- Test E2E : pyannote lancé en pré-résumé si `diarization_context.md` absent, puis rôles appliqués après l’étape mapping.

### Changed
- Worker interne sérialisé pour exécuter les traitements longs hors de la requête HTTP.
- Endpoint `/ready` pour distinguer l’état “process vivant” et “service prêt”.
- Endpoint `/metrics` enrichi avec la capacité et l’activité du worker.
- Script `scripts/bootstrap_config.py` pour générer un `config.yaml` prérempli à partir de l’environnement détecté.
- Workflow GitHub Actions pour exécuter la suite pytest.
- `SECURITY.md` pour documenter le signalement et le périmètre sécurité.
- L’API `POST /api/jobs/<id>/process` planifie désormais le traitement en arrière-plan et répond immédiatement.
- Les transitions de workflow liées au pré-traitement et au lancement de traitement sont centralisées.
- Le logging structuré conserve le contexte `job_id` et `step` hors requête Flask.
- La documentation active a été réalignée sur le mode service longue durée.

### Fixed
- `_apply_llm_suggestions()` : faux positif silencieux — le test `”indisponible” in summary_text.lower()` déclenchait un early return quand le résumé LLM mentionnait légitimement le mot “indisponible” dans son contenu (ex : “fallback quand X est indisponible”), laissant `meeting_context.json` non mis à jour malgré un résumé valide. Remplacé par une comparaison exacte à la sentinelle `”Résumé indisponible.”`.
- `_apply_llm_suggestions()` : suppression du double header `# Résumé de contrôle` dans `summary.md` — opencode écrit ce header lui-même, `_apply_llm_suggestions` n’ajoute plus que l’extrait de transcription en fin de fichier (et seulement s’il est non vide).
- Régression de supervision causée par l’absence de `/health` et `/metrics`.
- Gestion plus sûre des relances de jobs bloqués et des annulations.

# AGENTS.md — Guide pour agents de codage

> Ce fichier est le point d'entrée pour tout LLM de codage intervenant sur TranscrIA MVP.
> Lis-le intégralement avant de modifier le code.

## Commandes essentielles

```bash
# Installation
pip install -r requirements.txt

# Lancer l'application (dev)
python app.py

# Lancer l'application (production)
./start.sh    # log: /var/log/transcrIA.log, PID: /run/transcrIA.pid
./stop.sh
./status.sh

# Tests
python -m pytest tests/ -q

# Un seul fichier de test
python -m pytest tests/test_auth.py -v

# Lint / format — AUCUN linter configuré dans le projet.
# Le projet ne suit pas black/ruff/flake8. Respecte le style existant.
```

## Stack technique

- **Python 3.11+** avec annotations de type (`type | None`, pas `Optional`)
- **Flask 3.x** + Flask-Login + Flask-SQLAlchemy
- **Jinja2** pour les templates, **Bootstrap 5** pour le CSS
- **SQLAlchemy** avec SQLite (`transcrIA.db` dans le cwd)
- **PyYAML** pour la configuration
- **torch + transformers** pour Cohere ASR
- **pyannote.audio** pour la diarisation (optionnel, commenté dans `requirements.txt`)
- **opencode** (CLI externe) pour orchestrer Qwen 35B (résumé + arbitrage)

## Structure du projet

```
transcria-mvp/
  app.py                    # create_app() + main()
  config.yaml               # Configuration production (pas dans git)
  config.example.yaml       # Template de configuration
  transcria/
    config.py                # load_config / get_config / set_config (singleton)
    database.py              # db = SQLAlchemy()
    logging_setup.py         # RotatingFileHandler + stdout
    auth/                    # User, Role, Permission, UserStore, routes /login /admin/users
    jobs/                    # Job, JobState (20 états), JobStore, JobFilesystem
    workflow/                # WorkflowRunner, WorkflowState, WorkflowSteps
    audio/                   # AudioAnalyzer (ffprobe), AudioConverter (ffmpeg)
    stt/                     # CohereTranscriber, Transcriber, DiarizerService, SpeakerDetector, SummaryGenerator
    context/                 # MeetingContextManager, ParticipantsManager, LexiconManager, JobContextBuilder
    quality/                 # QualityReporter (9 checks, score /100)
    exports/                 # PackageBuilder (ZIP)
    integrations/            # DashboardClient, SrtEditorLink
    gpu/                     # VRAMManager (cycle GPU), OpenCodeRunner (opencode CLI)
    web/                     # routes.py (30+ endpoints) + templates/ + static/
  jobs/                      # Données runtime (1 sous-répertoire par job)
  configs/                   # prompts/ (summary, correction, arbitration, speaker_identification) + lexique_metier.txt
  tests/                     # 17 fichiers pytest
  docs/                      # TECHNICAL.md, BUGS.md, DATA_MODEL.md, CONFIG_REFERENCE.md
```

## Conventions de code

### Style
- Indentation : 4 espaces
- Longueur de lignes : pas de limite stricte, rester lisible
- Imports : stdlib → third-party → local, un par ligne
- Docstrings : format Google (`Args:`, `Returns:`) sur les fonctions publiques
- Pas de commentaires sauf si le code est non évident (IF/TODO pour complexité)
- Chaînes en français pour les messages utilisateur et la documentation
- Messages de log en français

### Nomenclature
- Fichiers Python : `snake_case.py`
- Templates Jinja2 : `snake_case.html`
- Classes : `PascalCase`
- Fonctions/méthodes publiques : `snake_case`
- Constantes : `UPPER_SNAKE_CASE`
- Variables de config YAML : `snake_case`

### Patterns récurrents
- **Config** : toujours via `get_config()`, jamais hardcoded. Les fonctions reçoivent `config: dict` en paramètre.
- **JobFilesystem** : créé à chaque opération (`fs = JobFilesystem(jobs_dir, job_id)`), pas de cache.
- **Store** : classes statiques (`JobStore.create_job()`, `UserStore.get_by_id()`), pas d'instances.
- **Routes web** : dans `web/routes.py` sur `web_bp`. Routes auth dans `auth/routes.py` sur `auth_bp`.
- **Blueprints** : `auth_bp` (prefix `/`), `web_bp` (prefix `/`)
- **Templates** : héritent de `base.html`, blocs `title`, `content`, `extra_head`

## Architecture clé

### Cycle GPU (VRAMManager)
L'application tourne sur un serveur avec 2 GPUs. Les modèles ne tiennent pas tous en mémoire simultanément :
1. **Cohere ASR** : ~6 Go VRAM sur 1 GPU
2. **pyannote** : ~2 Go VRAM sur 1 GPU
3. **Qwen 35B** : ~48 Go VRAM sur 2 GPUs

Le cycle est : Cohere→(offload)→pyannote→(offload+free GPUs)→Qwen→(offload). `VRAMManager` gère ce cycle, tue les processus GPU si besoin, et vérifie la disponibilité via le dashboard LLM (port 5001) ou `nvidia-smi` en fallback.

### Workflow (9 étapes affichées)
Le wizard guide l'utilisateur de l'upload au package ZIP. Chaque étape correspond à un `JobState`. Voir `docs/DATA_MODEL.md` pour les transitions.

### Stockage par job
Chaque job a un répertoire `jobs/<job_id>/` avec 7 sous-répertoires. Voir `docs/DATA_MODEL.md` pour l'arborescence complète et les fichiers produits à chaque étape.

### Config singleton
`get_config()` retourne un singleton chargé une fois. `set_config()` le met à jour en mémoire. Il n'y a PAS de `save_config()` pour écrire sur disque. Les modules qui appellent `get_config()` une seule fois au démarrage ne voient pas les mises à jour via `set_config()`.

## Pièges connus

### BUG-001 : 8 routes API sans vérification d'accès propriétaire
Les routes `api_analyze`, `api_summary`, `api_context`, `api_participants`, `api_lexicon`, `api_speakers_detect`, `api_speakers_map`, `api_process` n'appellent pas `_require_job_access()`. N'importe quel utilisateur authentifié accède aux jobs d'autrui.

### BUG-002 : Route push-to-editor sans décorateur @web_bp.route
`api_push_to_editor` (routes.py) n'a pas de `@web_bp.route(...)`. La route n'existe pas.

### BUG-003 : Variable speakers_map non définie
`Transcriber.transcribe()` ligne 36 référence `speakers_map` qui n'existe pas. Corriger en `speaker_mapping or {}`.

### BUG-012 : Titre du job écrasé par le nom du fichier
`api_upload` (routes.py:182) fait `job.title = file.filename`. Et `run_analyze` (runner.py:23) remplace par `result.get("format")`. Le titre utilisateur est perdu.

### BUG-015 : Données de diarization non transmises au LLM de résumé
Pyannote tourne avant le LLM dans `run_summary()`, mais ses résultats (nombre de locuteurs, temps de parole) ne sont pas inclus dans l'input du LLM. Voir `docs/BUGS.md` pour les détails complets.

### _STEPS incohérent (10 vs 9)
`workflow/steps.py` a `_STEPS` avec 10 entrées (speakers séparé), mais le workflow affiche 9 étapes (participants+speakers fusionnés). Les méthodes `get_step_index()` et `get_next_step_id()` utilisent les index de `_STEPS`.

### Cohere ne fait PAS de diarization
Cohere V2 est un modèle ASR pur. `CohereTranscriber.transcribe()` retourne `{start, end, text}` — **pas de `speaker`**. Les labels de locuteurs viennent uniquement de pyannote via `_apply_speakers()`.

### JobContextBuilder.build() appelé une seule fois
`JobContextBuilder.build()` n'est appelé qu'à l'étape 4 (speakers/map). Le fichier `job_context.yaml` n'existe pas avant cette étape. Les étapes qui le référencent (résumé, arbitrage) ne le trouvent pas.

### tests/ ne couvre pas les routes web
Les tests pytest couvrent les stores, la config, le contexte, la qualité, les exports, les edge cases. Les routes Flask sont testées via `test_web_api.py` mais les templates ne sont pas testés.

## Règles absolues

1. **Toujours** vérifier `_require_job_access(job, current_user)` dans les routes API qui modifient un job.
2. **Jamais** commit `config.yaml` (contient des chemins absolus de production).
3. **Toujours** passer `config: dict` en paramètre aux fonctions du moteur, jamais `get_config()` direct (sauf dans les routes).
4. **Ne pas** modifier `JobState` ou `WORKFLOW_STEPS` sans mettre à jour `WorkflowState.compute_statuses()`.
5. **Ne pas** ajouter de nouveaux fichiers JSON dans l'arborescence job sans documenter dans `DATA_MODEL.md`.
6. **Toujours** préserver les champs LLM dans `MeetingContextManager.save()` (la liste `llm_fields`).
7. **Ne pas** appeler `JobContextBuilder.build()` avant l'étape speakers/map (les fichiers participants/mapping n'existent pas).

## Documentation complémentaire

| Fichier | Contenu |
|---|---|
| `docs/TECHNICAL.md` | Architecture détaillée, flux de données, API REST, pipeline GPU |
| `docs/BUGS.md` | 15 bugs documentés avec causes racines et corrections proposées |
| `docs/DATA_MODEL.md` | Schéma de données, états, transitions, arborescence disque |
| `docs/CONFIG_REFERENCE.md` | Référence complète des paramètres config.yaml |
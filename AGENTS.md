# AGENTS.md — Guide pour agents de codage

> Ce fichier est le point d'entrée pour tout LLM de codage intervenant sur TranscrIA.
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
transcria/
  app.py                    # create_app() + main()
  config.yaml               # Configuration production (pas dans git)
  config.example.yaml       # Template de configuration
  transcria/
    config/                  # Package : loader.py, config_schema.py, system_detector.py
    database.py              # db = SQLAlchemy()
    logging_setup.py         # StructuredLogger (correlation_id, contexte, rotation)
    auth/                    # User, Role, Permission, UserStore, routes /login /admin/users
    jobs/                    # Job, JobState (20 états), JobStore, JobFilesystem
    workflow/                # WorkflowRunner, WorkflowState, WorkflowSteps
    audio/                   # AudioAnalyzer (ffprobe), AudioConverter (ffmpeg)
    stt/                     # BaseTranscriber (ABC), CohereTranscriber, WhisperTranscriber
    │                        # Transcriber, TranscriberFactory, DiarizerService, SpeakerDetector, SummaryGenerator
    context/                 # MeetingContextManager, ParticipantsManager, LexiconManager, JobContextBuilder
    quality/                 # QualityReporter (9 checks, score /100, seuils configurables)
    exports/                 # PackageBuilder (ZIP)
    integrations/            # DashboardClient, SrtEditorLink
    gpu/                     # VRAMManager, GPUSession, OpenCodeRunner, LLMBackend (script/ollama/http)
    services/                # JobService, PipelineService, ConfigService, JobExecutorService
    web/                     # routes.py (30+ endpoints) + templates/ + static/js/
  jobs/                      # Données runtime (1 sous-répertoire par job)
  configs/                   # prompts/ (summary, correction) + lexique_metier.txt
  scripts/                   # scripts shell + bootstrap_config.py
  tests/                     # 20+ modules pytest
  docs/                      # TECHNICAL.md, DATA_MODEL.md, CONFIG_REFERENCE.md, INSTALL.md
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

### Modèle service/worker
Le portail web ne doit plus exécuter les traitements longs directement dans la requête HTTP.
`/api/jobs/<id>/process` planifie le traitement, puis `JobExecutorService` l'exécute en arrière-plan avec un worker sérialisé par défaut (`workflow.execution.max_concurrent_jobs=1`).
La supervision du service passe par `/health`, `/ready` et `/metrics`.

### Stockage par job
Chaque job a un répertoire `jobs/<job_id>/` avec 7 sous-répertoires. Voir `docs/DATA_MODEL.md` pour l'arborescence complète et les fichiers produits à chaque étape.

### Config singleton
`get_config()` retourne un singleton chargé une fois. `set_config()` le met à jour en mémoire. `save_config()` écrit sur disque. Les modules qui appellent `get_config()` une seule fois au démarrage ne voient pas les mises à jour via `set_config()`.

## Pièges connus

### Cohere ne fait PAS de diarization
Cohere V2 est un modèle ASR pur. `CohereTranscriber.transcribe()` retourne `{start, end, text}` — **pas de `speaker`**. Les labels de locuteurs viennent uniquement de pyannote via `_apply_speakers()`.

### `job_context.yaml` n'est pas garanti avant toutes les phases LLM
Le résumé LLM tente de lire `context/job_context.yaml`, mais ce fichier n'est construit qu'après certaines étapes de saisie (`lexicon`, `speakers/map`). Le code gère ce cas en tolérant un chemin absent, mais il ne faut pas supposer sa présence avant le mapping locuteurs ou le lexique.

### tests/ couvre bien le métier, moins les intégrations lourdes
Les tests pytest couvrent les stores, la config, le contexte, la qualité, les exports, les routes Flask (`test_web_api.py`, `test_web_edge_cases.py`) et le workflow, y compris le worker interne et les transitions. En revanche, beaucoup de tests mockent encore les dépendances GPU/LLM, donc certains bugs d'intégration passent sous le radar.

## Règles absolues

1. **Toujours** vérifier `_require_job_access(job, current_user)` dans les routes API qui modifient un job.
2. **Jamais** commit `config.yaml` (contient des chemins absolus de production).
3. **Toujours** passer `config: dict` en paramètre aux fonctions du moteur, jamais `get_config()` direct (sauf dans les routes).
4. **Ne pas** modifier `JobState` ou `WORKFLOW_STEPS` sans mettre à jour `WorkflowState.compute_statuses()`.
5. **Ne pas** ajouter de nouveaux fichiers JSON dans l'arborescence job sans documenter dans `DATA_MODEL.md`.
6. **Toujours** préserver les champs LLM dans `MeetingContextManager.save()` (la liste `llm_fields`).
7. **Toujours** garder cohérents `meeting_context.json` et `job_context.yaml/json` quand un champ alimente le LLM de correction.
8. **Toujours** protéger les endpoints système JSON avec les mêmes permissions que les pages HTML équivalentes.
9. **Toujours** passer par `workflow/transitions.py` pour la logique de lancement/annulation/reprise de traitement.

## Documentation complémentaire

| Fichier | Contenu |
|---|---|
| `docs/TECHNICAL.md` | Architecture détaillée, flux de données, API REST, pipeline GPU |
| `docs/DATA_MODEL.md` | Schéma de données, états, transitions, arborescence disque |
| `docs/CONFIG_REFERENCE.md` | Référence complète des paramètres config.yaml |
| `docs/INSTALL.md` | Guide d'installation complet |

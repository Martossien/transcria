# AGENTS.md — Guide pour agents de codage

> Ce fichier est le point d'entrée pour tout LLM de codage intervenant sur TranscrIA.
> Lis-le intégralement avant de modifier le code.

## Commandes essentielles

```bash
# Installation complète (méthode recommandée)
./install.sh                         # Venv, PyTorch, dépendances, config, service systemd
./install.sh --no-service --no-torch # Réinstallation partielle

# Installation manuelle (si install.sh non adapté)
python3 -m venv venv && source venv/bin/activate
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu126
pip install -r requirements.txt
pip install -r requirements-dev.txt  # pytest, pytest-cov
python scripts/bootstrap_config.py --output config.yaml

# Lancer l'application (dev)
source venv/bin/activate
python app.py

# Lancer l'application (production)
./start.sh    # log: /var/log/transcrIA.log, PID: /run/transcrIA.pid
./stop.sh
./status.sh

# Tests
python -m pytest tests/ -q           # 412 tests (22 modules, mock, pas de GPU requis)
python -m pytest tests/test_auth.py -v
python tests/test_e2e_workflow.py --skip-llm   # E2E rapide (1 GPU)
python tests/test_e2e_workflow.py              # E2E complet (GPUs + LLM requis)

# Lint / format — AUCUN linter configuré dans le projet.
# Le projet ne suit pas black/ruff/flake8. Respecte le style existant.
```

## Stack technique

- **Python 3.11+** avec annotations de type (`type | None`, pas `Optional`)
- **Flask 3.x** + Flask-Login + Flask-SQLAlchemy
- **Jinja2** pour les templates, **Bootstrap 5** pour le CSS
- **SQLAlchemy** avec SQLite (`transcrIA.db` dans le cwd)
- **PyYAML** pour la configuration
- **python-dotenv** pour charger `.env` au démarrage (`app.py`)
- **torch + transformers + accelerate** pour Cohere ASR (device_map GPU)
- **pyannote.audio** pour la diarisation (dans `requirements.txt`, modèle téléchargé séparément)
- **opencode** (CLI externe) pour orchestrer Qwen 35B (résumé + arbitrage)

## Structure du projet

```
transcria/
  app.py                    # create_app() + main()
  install.sh                # Script d'installation guidée (venv, PyTorch, config, systemd)
  config.yaml               # Configuration production (pas dans git)
  config.example.yaml       # Template de configuration
  requirements.txt          # Dépendances runtime
  requirements-dev.txt      # Dépendances dev (pytest, pytest-cov)
  transcria.service         # Unité systemd (adapter les chemins avant installation)
  start.sh / stop.sh / status.sh
  transcria/
    config/
      loader.py             # load_config(), get_config(), save_config(), _deep_merge()
      config_schema.py      # validate_config(), ValidationResult
      system_detector.py    # SystemDetector.detect() — GPUs, binaires, RAM, disque
    database.py             # db = SQLAlchemy()
    logging_setup.py        # StructuredLogger (correlation_id, contexte, rotation)
    auth/
      models.py             # User, Role
      permissions.py        # Permission (enum), décorateurs de permission
      store.py              # UserStore — méthodes statiques
      routes.py             # auth_bp : /login, /logout, /admin/users
    jobs/
      models.py             # Job, JobState (20 états)
      store.py              # JobStore — méthodes statiques
      filesystem.py         # JobFilesystem — arborescence disque par job
    workflow/
      states.py             # WorkflowState.compute_statuses()
      steps.py              # WORKFLOW_STEPS (9 étapes)
      runner.py             # WorkflowRunner — exécution des étapes
      transitions.py        # logique lancement / annulation / reprise
    audio/
      analyzer.py           # AudioAnalyzer (ffprobe)
      converter.py          # AudioConverter (ffmpeg)
    stt/
      base_transcriber.py   # BaseTranscriber (ABC)
      cohere_transcriber.py # CohereTranscriber — Cohere ASR (device_map, GPU)
      whisper_transcriber.py# WhisperTranscriber — faster-whisper large-v3
      transcriber_factory.py# TranscriberFactory — sélection backend selon config
      transcription.py      # Transcriber (orchestration)
      diarization.py        # DiarizerService — pyannote
      speaker_detection.py  # SpeakerDetector
      summary.py            # SummaryGenerator — résumé rapide LLM
    context/
      meeting_context.py    # MeetingContextManager
      participants.py       # ParticipantsManager
      lexicon.py            # LexiconManager
      job_context_builder.py# JobContextBuilder — assemble job_context.yaml/json
    quality/
      quality_report.py     # QualityReporter (9 checks, score /100)
      srt_checks.py         # Checks sur le SRT
      lexicon_checks.py     # Checks sur le lexique
      review_points.py      # Points de relecture
    exports/
      package_builder.py    # PackageBuilder — ZIP final
    integrations/
      dashboard_client.py   # DashboardClient (port 5001)
      srt_editor_link.py    # SrtEditorLink (port 7861)
    gpu/
      vram_manager.py       # VRAMManager — orchestration cycle GPU
      gpu_session.py        # GPUSession — context manager
      llm_backend.py        # LLMBackend (script/ollama/http)
      opencode_runner.py    # OpenCodeRunner — exécute opencode CLI
    services/
      job_executor.py       # JobExecutorService — worker interne (thread)
      job_service.py        # JobService
      pipeline_service.py   # PipelineService
      config_service.py     # ConfigService
    web/
      routes.py             # web_bp : 28 endpoints (pages + API JSON)
      templates/            # base.html + templates par étape
      static/js/            # wizard.js, wizard-api.js
  jobs/                     # Données runtime (1 sous-répertoire par job)
  configs/
    prompts/                # Prompts LLM (summary.md, correction.md)
    lexique_metier.txt      # Lexique métier global
  scripts/
    bootstrap_config.py     # Génère config.yaml depuis config.example.yaml + auto-détection
    launch_arbitrage.sh     # Lance llama-server (Qwen 35B, 2 GPUs, contexte 263K)
    stop_qwen.sh            # Arrête llama-server proprement
    stop_qwen_vllm.sh       # Arrête vLLM (si utilisé à la place de llama.cpp)
  tests/                    # 22 modules pytest, 412 tests (mocks GPU/LLM)
    conftest.py
    test_e2e_workflow.py    # Test E2E complet avec GPU réels
    E2E_README.md
  docs/
    INSTALL.md              # Guide d'installation (install.sh, venv, modèles, systemd)
    TECHNICAL.md            # Architecture, flux de données, API REST, pipeline GPU
    DATA_MODEL.md           # États, transitions, arborescence disque par job
    CONFIG_REFERENCE.md     # Référence complète des paramètres config.yaml
```

## Conventions de code

### Style
- Indentation : 4 espaces
- Longueur de lignes : pas de limite stricte, rester lisible
- Imports : stdlib → third-party → local, un par ligne
- Docstrings : format Google (`Args:`, `Returns:`) sur les fonctions publiques
- Pas de commentaires sauf si le code est non évident
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
L'application tourne sur un serveur avec plusieurs GPUs NVIDIA. Les modèles ne tiennent pas tous en mémoire simultanément :
1. **Cohere ASR** : ~6 Go VRAM
2. **pyannote** : ~2 Go VRAM
3. **Qwen 35B** : ~48 Go VRAM sur 2 GPUs

Le cycle est : Cohere→(offload)→pyannote→(offload+free GPUs)→Qwen→(offload). `VRAMManager` gère ce cycle, vérifie la disponibilité via le dashboard LLM (port 5001) ou `nvidia-smi` en fallback.

### Workflow (9 étapes affichées)
Le wizard guide l'utilisateur de l'upload au package ZIP. Chaque étape correspond à un `JobState`. Les transitions passent obligatoirement par `workflow/transitions.py`. Voir `docs/DATA_MODEL.md` pour le détail des états.

### Modèle service/worker
`/api/jobs/<id>/process` planifie le traitement ; `JobExecutorService` l'exécute en arrière-plan (worker sérialisé, `workflow.execution.max_concurrent_jobs=1`). Supervision : `/health`, `/ready`, `/metrics`.

### Config singleton
`get_config()` retourne un singleton chargé une fois au démarrage. `set_config()` le met à jour en mémoire. `save_config()` écrit sur disque. Les modules qui capturent `get_config()` au démarrage ne voient pas les mises à jour ultérieures.

### Installation et bootstrap
`install.sh` orchestre l'installation complète. `scripts/bootstrap_config.py` génère `config.yaml` en fusionnant `config.example.yaml` avec les valeurs auto-détectées (`SystemDetector` : GPUs, binaires, chemins). Le fichier `.env` porte les secrets (`TRANSCRIA_SECRET`, `HF_TOKEN`).

## Pièges connus

### Cohere ne fait PAS de diarization
`CohereTranscriber.transcribe()` retourne `{start, end, text}` — **pas de `speaker`**. Les labels de locuteurs viennent uniquement de pyannote via `_apply_speakers()`.

### `job_context.yaml` n'est pas garanti avant toutes les phases LLM
Le résumé LLM tente de lire `context/job_context.yaml`, mais ce fichier n'est construit qu'après le mapping locuteurs et le lexique. Le code tolère un chemin absent — ne pas supposer sa présence avant ces étapes.

### Mode debug et speechbrain/k2_fsa
`server.debug: true` active le reloader Werkzeug, qui recharge les modules CUDA et provoque un crash avec `speechbrain`/`k2_fsa` (importés par pyannote). **Toujours garder `debug: false` en production.**

### tests/ couvre le métier, moins les intégrations GPU
412 tests dans 22 modules couvrent stores, config, contexte, qualité, exports, routes Flask et workflow. La plupart mockent les dépendances GPU/LLM. `test_e2e_workflow.py` requiert un vrai GPU.

## Règles absolues

1. **Toujours** vérifier `_require_job_access(job, current_user)` dans les routes API qui modifient un job.
2. **Jamais** committer `config.yaml` (contient des chemins absolus de production) ni `.env` (secrets).
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
| `docs/INSTALL.md` | Guide d'installation complet (install.sh, venv, modèles, service systemd, dépannage) |
| `docs/TECHNICAL.md` | Architecture détaillée, flux de données, API REST, pipeline GPU |
| `docs/DATA_MODEL.md` | Schéma de données, états, transitions, arborescence disque |
| `docs/CONFIG_REFERENCE.md` | Référence complète des paramètres config.yaml |

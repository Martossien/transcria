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
# Adapter le tag CUDA au driver local (cu121/cu124/cu126) ou utiliser ./install.sh --cuda.
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu126
pip install -r requirements.txt
pip install -r requirements-dev.txt  # pytest, pytest-cov
python scripts/bootstrap_config.py --output config.yaml

# Lancer l'application (dev)
source venv/bin/activate
python app.py

# Lancer l'application (production — service systemd)
sudo systemctl restart transcria.service   # redémarre proprement
sudo systemctl stop transcria.service
sudo systemctl status transcria.service
sudo truncate -s 0 /var/log/transcrIA.log  # remet le log à zéro (débogage)

# Scripts legacy (si systemd non disponible)
./start.sh    # log: /var/log/transcrIA.log, PID: /run/transcrIA.pid
./stop.sh
./status.sh

# Tests — ⚠️ TOUJOURS via le venv (python système = pas de python-docx → 21 faux échecs)
venv/bin/python -m pytest tests/ -q              # suite mockée majoritaire, pas de GPU requis
venv/bin/python -m pytest tests/test_auth.py -v

# CI (.github/workflows/tests.yml) — 3 gates, reproductibles en local :
ruff check transcria/ inference_service/ --line-length 140 --select E,W,F,I
mypy transcria/ inference_service/ --ignore-missing-imports
venv/bin/python -m pytest tests/ -q --cov=transcria --cov-fail-under=65   # seuil 65 % (actuel ~77 %)
# Tests réseau (faux serveurs sur vrai socket) : marqueur "integration" → -m integration / -m "not integration"
# ⚠️  Tests E2E : TOUJOURS utiliser le python du venv (pyannote et Cohere n'y sont que là)
venv/bin/python tests/test_e2e_workflow.py --skip-llm               # E2E rapide (1 GPU)
venv/bin/python tests/test_e2e_workflow.py                          # E2E complet (GPUs + LLM requis)
venv/bin/python tests/test_e2e_workflow.py --keep                   # Conserve le job pour inspection
venv/bin/python tests/test_e2e_workflow.py --audio tests/test2.mp3  # Autre fichier audio

# Lint / format (cf. CI ci-dessus pour les commandes exactes qui gatent)
# black n'est PAS utilisé. Respecte le style du fichier que tu modifies.
```

## Stack technique

- **Python 3.11+** avec annotations de type (`type | None`, pas `Optional`)
- **Flask 3.x** + Flask-Login + Flask-SQLAlchemy
- **Jinja2** pour les templates, **Bootstrap 5** pour le CSS
- **SQLAlchemy** sur **PostgreSQL** (Phase A) via `psycopg` + **Alembic** (migrations) ; DSN dans `TRANSCRIA_DATABASE_URL`. SQLite (`transcrIA.db`) reste un repli mono-process pour le dev/les tests
- **PyYAML** pour la configuration
- **python-dotenv** pour charger `.env` au démarrage (`app.py`)
- **torch + transformers + accelerate** pour Cohere ASR et Granite Speech expérimental (device_map GPU)
- **faster-whisper** pour Whisper large-v3 qualité, VAD Silero et timestamps mot-à-mot
- **NeMo** (`nemo_toolkit[asr]`) pour Parakeet TDT 0.6B v3 expérimental (ASRModel.from_pretrained, pas de device_map)
- **pyannote.audio** pour la diarisation (dans `requirements.txt`, modèle téléchargé séparément)
- **opencode** (CLI externe) pour orchestrer la LLM locale d'arbitrage (résumé + correction)

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
      models.py             # User, Role, Group, GroupMembership, GroupRole
      permissions.py        # Permission (enum), décorateurs de permission
      store.py              # UserStore — méthodes statiques
      groups.py             # GroupStore — groupes, membres, admins de groupe
      routes.py             # auth_bp : /login, /logout, /admin/users, /admin/groups
    jobs/
      models.py             # Job, JobState (20 états)
      store.py              # JobStore — méthodes statiques
      filesystem.py         # JobFilesystem — arborescence disque par job
    queue/
      allocator.py          # GPUAllocator — réservations GPU atomiques par job/phase + verrou LLM + PID tracking
      store.py              # QueueStore — file persistante, priorités, pause/reprise, aging
      scheduler.py          # QueueScheduler — dispatch en arrière-plan selon capacité/calendrier
      calendar.py           # SchedulingCalendar — pause_queue, limit_concurrency, force_gpu
      routes.py             # /admin/queue, /admin/schedule, /api/queue/*, /api/schedule/*
    workflow/
      states.py             # WorkflowState.compute_statuses()
      steps.py              # WORKFLOW_STEPS (9 étapes)
      progress.py           # WorkflowProgressReporter — progression UI persistée dans jobs.extra_data_json["workflow_progress"]
      runner.py             # WorkflowRunner — exécution des étapes
      transitions.py        # logique lancement / annulation / reprise
    audio/
      analyzer.py           # AudioAnalyzer (ffprobe)
      converter.py          # AudioConverter (ffmpeg)
      preflight.py           # AudioPreflightAnalyzer — pré-diagnostic acoustique (RMS, SNR, bande passante, clipping, flags)
      vad.py                # SileroVAD — détection de parole via faster_whisper
      vad_adaptive.py       # AdaptiveVADConfig — seuils VAD selon qualité audio
      vad_hysteresis.py      # HysteresisBinarizer — post-traitement hystérésis des scores VAD
      scene_analyzer.py     # AudioSceneAnalyzer — orchestrateur subprocess analyse de scène
      _scene_analysis_worker.py # Worker subprocess : pipeline RMS→flatness/ZCR→pitch YIN (librosa)
      scene_filter.py       # AudioSceneFilterService — silence optionnel des zones non vocales sans décaler les timestamps
      denoise.py             # AudioDenoiseService — débruitage ffmpeg expérimental (afftdn, désactivé par défaut)
      normalization.py       # AudioNormalizationService — normalisation ffmpeg optionnelle, auto-loudnorm si RMS < seuil, weak_voice
      source_separation.py  # SourceSeparationDecider + SourceSeparationService — separation vocaux/instrumentaux (demucs, désactivé par défaut)
    stt/
      base_transcriber.py   # BaseTranscriber (ABC)
      cohere_transcriber.py # CohereTranscriber — Cohere ASR (AutoModelForSpeechSeq2Seq, numpy array)
      whisper_transcriber.py# WhisperTranscriber — faster-whisper large-v3 qualité
      granite_transcriber.py# GraniteTranscriber — IBM Granite Speech 4.1 2B expérimental
      parakeet_transcriber.py# ParakeetTranscriber — NVIDIA Parakeet TDT 0.6B v3 expérimental (NeMo, auto-détection langue, timestamps natifs)
      anti_hallucination.py # Détection/réduction boucles répétitives ASR
      lexicon_hotwords.py   # Construction hotwords Whisper depuis lexique de session (option expérimentale)
      contextual_biasing.py # Trie/LogitsProcessor expérimental Cohere depuis lexique de session
      forced_alignment.py   # Alignement CTC natif torchaudio optionnel
      speaker_realignment.py# Réalignement locuteur/ponctuation au niveau mot
      reliability.py          # SegmentReliabilityScorer — scoring fiabilité post-STT (ok/suspect/degrade)
      transcriber_factory.py# TranscriberFactory — sélection backend selon config
      transcription.py      # Transcriber — chunking pyannote/30s + alignement + realignment + _cleanup_transcription_segments() (artefacts + micro-segments)
      base_diarizer.py      # BaseDiarizer (ABC) — interface commune + méthodes partagées (cache, clips, embeddings, fingerprint)
      diarization.py        # DiarizerService(BaseDiarizer) — backend pyannote + hook progress logué + exclusive_speaker_diarization + pipeline_params expérimentaux + checkpoints
      sortformer_diarizer.py# SortformerDiarizer(BaseDiarizer) — NVIDIA Sortformer 4spk v2.1 expérimental (NeMo, language-agnostic, max 4 locuteurs, chargement HF ou `.nemo` local via `_find_nemo_file`)
      diarizer_factory.py   # create_diarizer(), get_diarizer_vram_mb(), list_available_backends() — sélection backend selon models.diarization_backend ; apply_speaker_hint() applique la fourchette de locuteurs du job (+ guard Sortformer ≤ 4)
      remote_transcriber.py # RemoteTranscriber(BaseTranscriber) — STT distant (protocole OpenAI, concurrent_safe)
      remote_diarizer.py    # RemoteDiarizer(BaseDiarizer) — diarisation distante via inference_service
      speaker_detection.py  # SpeakerDetector
      summary.py            # SummaryGenerator — VAD pré-transcription + backend STT configuré
    context/
      meeting_context.py    # MeetingContextManager + MEETING_TYPES (18 types) + TYPE_SPECIFIC_FIELDS (champs par type)
      participants.py       # ParticipantsManager
      lexicon.py            # LexiconManager (20 catégories, variants, contexts)
      central_lexicon_models.py # Modèles SQLAlchemy lexiques centralisés par groupe
      central_lexicon_store.py  # Permissions, CRUD, import et périmètre job→groupe
      central_lexicon_service.py# Fusion session/LLM/central + filtrage avant correction
      central_lexicon_routes.py # Routes admin /admin/lexicons
      job_context_builder.py# JobContextBuilder — assemble job_context.yaml/json
    quality/
      audio_quality.py      # AudioQualityEvaluator — décision Cohere/Whisper selon diagnostics
      quality_report.py     # QualityReporter (16 checks, score /100)
      srt_checks.py         # Checks sur le SRT
      lexicon_checks.py     # Checks sur le lexique
      review_points.py      # Points de relecture
    exports/
      package_builder.py    # PackageBuilder — ZIP final (inclut le rapport DOCX)
      docx_report.py        # DocxReport — rapport Word pro adapté au type : extraction structurée (décisions/actions/votes…),
                            #   champs type-spécifiques, thèmes visuels par type (_DocxTheme), quorum CSE auto.
                            #   generate_docx_report(job_id, jobs_dir, output_path). Exclu de mypy (python-docx sans stubs).
    integrations/
      dashboard_client.py   # DashboardClient (port 5001)
      srt_editor_link.py    # SrtEditorLink (port 7861)
    gpu/
      vram_manager.py       # VRAMManager — orchestration cycle GPU
      gpu_session.py        # GPUSession — context manager
      llm_backend.py        # LLMBackend (script/ollama/http)
      opencode_runner.py    # OpenCodeRunner — exécute opencode CLI
      opencode_setup.py     # find_opencode_binary() + ensure_local_provider() — config opencode.json fiable/idempotente
      _port_utils.py        # is_port_open() partagé entre vram_manager et llm_backend
      cuda_visible.py       # parse_cuda_visible_devices(), to_visible_device_index(), to_nvidia_smi_gpu_index()
      stt_vram_planner.py   # SttVramPlanner — pré-check VRAM (fraction×total vLLM) + relocalisation GPU
      stt_engine_supervisor.py # SttEngineSupervisor — cycle de vie A/B/C des moteurs STT distants (+ /engines/ensure)
    inference/
      client.py             # InferenceClient — service Flask distant (diarize/voice-embed, /capabilities, /engines/ensure)
      asr_client.py         # AsrClient — endpoint OpenAI /v1/audio/transcriptions (vLLM/SGLang, non hardcodé)
      resource_status.py    # remote_requirements(), assess_admission() (§7.2), summarize_capabilities()
      resource_gate.py      # prepare_remote_resources() — pré-vol admission + auto-lancement STT
    notifications/
      __init__.py
      mailer.py             # EmailConfig, build_email_config(), send_job_notification_async() — SMTP fire-and-forget daemon thread
    services/
      job_executor.py       # JobExecutorService — worker interne (thread) + _notify() hook email après COMPLETED/FAILED
      job_service.py        # JobService
      pipeline_service.py   # PipelineService — preflight, scene, quality refresh, source sep, filter, denoise, norm avant STT
      config_service.py     # ConfigService
    audit/
      models.py             # AuditAction + AuditLog (SQLAlchemy)
      store.py              # AuditStore — log(), query(), count(), purge_expired()
      decorator.py          # audit_log() + @audit_action — capture auto current_user + IP
      routes.py             # audit_bp : /admin/audit (filtres + export CSV)
    voice/
      models.py             # SQLAlchemy voix enregistrées, consentements, profils, matches, audit
      store.py              # VoiceStore — périmètre groupe, consentements, profils, audit
      embedding.py          # Empreintes vocales pyannote + sérialisation/cosine
      enrollment.py         # Génération profil depuis audio de référence avec suppression source par défaut
      matching.py           # Matching job→voix connues depuis clips locuteurs, suggestions validables
      routes.py             # voice_bp : /admin/voices + consentements + vectorisation
    web/
      routes.py             # web_bp : 30 routes (pages + API JSON)
      templates/            # base.html + templates par étape
      static/js/            # wizard.js, wizard-api.js
  inference_service/        # Service Flask « nœud de ressources » (diarize/voice-embed in-process A/B/C)
    app.py                  # create_app() + garde clé API sur /infer/* et /engines/*
    engine.py / diarize_engine.py # moteurs in-process (CAS A/B/C, idle-offload)
    capabilities.py         # build_capabilities() (pur)
    routes/                 # health, capabilities, engines (/engines/ensure), voice_embed, diarize
  jobs/                     # Données runtime (1 sous-répertoire par job)
  configs/
    prompts/                # Prompts LLM (summary_prompt.txt, correction_prompt.txt)
    lexique_metier.txt      # Lexique métier global
  scripts/
    bootstrap_config.py     # Génère config.yaml depuis config.example.yaml + auto-détection
    launch_arbitrage.sh     # Lance le backend LLM local configuré (llama-server par défaut)
    stop_llm_backend.sh     # Arrêt générique par port, PID file ou pattern explicite
    stop_arbitrage_llm.sh   # Wrapper d'arrêt standard de la LLM d'arbitrage
    stop_qwen.sh            # Wrapper legacy vers stop_arbitrage_llm.sh
    stop_qwen_vllm.sh       # Wrapper legacy vLLM via stop_llm_backend.sh
    check_arbitrage_llm.sh  # Diagnostic : modèle actif, test d'inférence, cohérence config
    setup_opencode.py       # Configure opencode.json (provider local) de façon idempotente — cf. opencode_setup.py
    bench_audio.py          # Orchestrateur benchmark multi-GPU, matrices STT/VAD/Cohere/Pyannote
    bench_analyze.py        # Analyse locale sans LLM (hallucinations, timing, comparatif)
    bench_eval.py           # Évaluation LLM des SRTs (nécessite la LLM d'arbitrage)
    _stt_serve_lib.sh       # Lib commune des lanceurs STT (moteur non hardcodé : STT_ENGINE vllm|sglang|custom)
    launch_stt_cohere.sh / launch_stt_whisper.sh / launch_stt_granite.sh # Lanceurs moteurs STT (vLLM par défaut)
    stop_stt.sh             # Arrêt par port via ss (groupe de process), liste STT_STOP_PORTS
    test_stt.sh             # Smoke endpoint STT (auto-convertit MP3→WAV)
    smoke_remote_stt.py     # Smoke E2E RemoteTranscriber contre un vrai serveur STT
  tests/                    # modules test_*.py + E2E (mocks GPU/LLM majoritaires) — 870+ tests
    conftest.py
    test_e2e_workflow.py    # Test E2E complet avec GPU réels
    test_mailer.py          # 20 tests — EmailConfig, templates, async dispatch, modes SMTP
    test_web_helpers.py     # 13 tests — helpers web (audio diagnostic, lexique, locuteurs)
    E2E_README.md
  docs/
    INSTALL.md              # Guide d'installation (install.sh, venv, modèles, systemd)
    TECHNICAL.md            # Architecture, flux de données, API REST, pipeline GPU
    DATA_MODEL.md           # États, transitions, arborescence disque par job
    CONFIG_REFERENCE.md     # Référence complète des paramètres config.yaml
    SERVICE_RESSOURCES_GPU.md # Inférence distante v1 : topologies, autonomie VRAM STT, /capabilities, mode dégradé
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
- **JobFilesystem** : créé à chaque opération (`fs = JobFilesystem(jobs_dir, job_id)`), pas de cache. `save_json()`/`save_text()` sont **atomiques** (fichier temporaire unique → `fsync` → `os.replace`) : un lecteur concurrent voit toujours l'ancien fichier complet ou le nouveau, jamais un contenu tronqué. Ne pas revenir à un `open(path, "w")` direct.
- **Store** : classes statiques (`JobStore.create_job()`, `UserStore.get_by_id()`), pas d'instances.
- **Routes web** : dans `web/routes.py` sur `web_bp`. Routes auth dans `auth/routes.py` sur `auth_bp`.
- **Blueprints** : `auth_bp` (prefix `/`), `web_bp` (prefix `/`)
- **Templates** : héritent de `base.html`, blocs `title`, `content`, `extra_head`

## Architecture clé

### Cycle GPU (VRAMManager)
L'application tourne sur un serveur avec plusieurs GPUs NVIDIA. Les modèles ne tiennent pas tous en mémoire simultanément :
1. **Cohere ASR** : ~6 Go VRAM
2. **Whisper large-v3 qualité** : ~10 Go VRAM selon compute_type
3. **Granite Speech 4.1 2B** : ~6 Go VRAM, expérimental et désactivé par défaut
4. **pyannote** : ~2 Go VRAM
5. **Parakeet TDT 0.6B v3** : ~8 Go VRAM, expérimental et désactivé par défaut
6. **LLM d'arbitrage locale** : VRAM variable selon modèle/backend/script (ex: 48–60 Go pour un 35B quantifié)

**`GPUSession`** est le context manager utilisé pour Cohere, Whisper, pyannote et Parakeet. Il appelle `ensure_free()` → scanne tous les GPUs → sélectionne le meilleur (VRAM libre max) → logue le GPU choisi → libère via `offload_all()` à la sortie. Ne pas hardcoder `cuda:0` — utiliser `GPUSession` ou `ensure_free()`.

`CUDA_VISIBLE_DEVICES` est supporté : les ids physiques remontés par le dashboard/nvidia-smi sont remappés vers les ordinaux CUDA visibles avant de construire `cuda:N`. Si `CUDA_VISIBLE_DEVICES=-1`, aucun GPU ne doit être sélectionné. La libération VRAM ciblée doit respecter le GPU visible demandé et les patterns `workflow.scheduling.kill_patterns`; ne pas tuer des processus GPU génériques hors liste.

**Note NeMo (Parakeet) :** `ASRModel.from_pretrained()` ignore `device_map` et charge sur `cuda:0` par défaut. `ParakeetTranscriber.load()` appelle `torch.cuda.set_device()` avant le chargement pour forcer le GPU cible.

**`ensure_arbitrage_llm_ready(expected_model_id)`** est le point d'entrée unique avant tout usage de la LLM d'arbitrage. Elle vérifie l'état réel du serveur (`/v1/models` + inférence test) et choisit parmi trois chemins logués explicitement :
- **CAS A** : LLM active et bon modèle → réutilisation directe, zéro redémarrage
- **CAS B** : LLM active mais mauvais modèle → redémarrage (warning logué)
- **CAS C** : LLM absente ou non saine → libération GPU + lancement depuis zéro

**Cycle de vie LLM** : chaque étape appelle uniquement `ensure_arbitrage_llm_ready()`. L'arrêt (`stop_arbitrage_llm()`) est fait **une seule fois** en fin de pipeline par `PipelineService._release_arbitrage_llm()`, qui vérifie d'abord `is_arbitrage_llm_running()` avant d'agir. `is_arbitrage_llm_running()` doit tester l'API OpenAI-compatible (`/v1/models` + inférence) avant tout fallback port/PID : `lsof` seul peut produire de faux négatifs sous systemd/sandbox. Ainsi la LLM reste vivante entre le résumé et la correction (CAS A garanti pour la correction si le résumé l'a démarrée).

`services.arbitrage_api_model_id` dans `config.yaml` doit correspondre à l'alias rapporté par le serveur (lancer `scripts/check_arbitrage_llm.sh` pour vérifier). `services.arbitrage_llm_port` remplace `qwen_port` pour les nouvelles configs. `services.llm_cleanup_ports` remplace `vllm_port` et liste les ports de backends LLM concurrents à libérer avant lancement. Les anciens noms restent lus par compatibilité. `free_all_gpus()` reste disponible pour les resets forcés uniquement.

Les références `qwen_*` encore présentes sont des aliases de compatibilité ancienne version ou des exemples de modèle local. Ne pas introduire de nouvelle dépendance fonctionnelle au nom Qwen : le contrat applicatif est "LLM d'arbitrage OpenAI-compatible configurée".

### Inférence distante (frontale + nœud de ressources)

TranscrIA peut tourner **tout-en-un** (ressources GPU locales, mode historique) ou en **frontale** dont les ressources GPU sont sur un **nœud distant**. Activé par la section `inference` de la config (`mode: local | remote | hybrid`). Détail complet : `docs/SERVICE_RESSOURCES_GPU.md`.

- **STT distant** : `RemoteTranscriber` (`transcria/stt/remote_transcriber.py`) parle le protocole **OpenAI** `/v1/audio/transcriptions` via `AsrClient` (`transcria/inference/asr_client.py`) — moteur de serving **non hardcodé** (vLLM, SGLang…). Sélection par `transcriber_factory._should_use_remote_stt` (mode remote/hybrid + `inference.stt.backends[<backend>].url`). `response_format` par backend (Cohere refuse `verbose_json` → `json`). Conversion WAV 16k mono systématique (l'endpoint rejette le MP3). Concurrence par tour via `inference.stt.concurrency` (>1, backends `concurrent_safe`).
- **Diarisation / empreinte vocale distantes** : `RemoteDiarizer`, `RemoteVoiceEmbeddingBackend` + service Flask `inference_service/` (routes `/infer/diarize`, `/infer/voice-embed`). Transport `inference.transport.audio` : `upload` OBLIGATOIRE en vrai distant (`file_ref` n'est valable qu'en filesystem partagé).
- **Autonomie VRAM du STT** (cycle A/B/C comme la LLM d'arbitrage, sans être intrusif) : `SttVramPlanner` (`transcria/gpu/stt_vram_planner.py`, sémantique vLLM = fraction × VRAM totale, pas la taille modèle) + `SttEngineSupervisor` (`transcria/gpu/stt_engine_supervisor.py`). L'admin décide du **placement** (manifeste `resource_node.engines`, scripts `launch_stt_*.sh`) ; le service décide du **quand** (réutilise / lance à la demande via `POST /engines/ensure` / 503 si saturé). `GET /capabilities` expose l'inventaire (GPU, VRAM, moteurs + santé).
- **Mode dégradé (admission §7.2)** : `resource_gate.prepare_remote_resources()` branché en pré-vol de `PipelineService.run_process` — nœud joignable → poursuit (+ ensure STT) ; injoignable → file (transitoire) ou échec explicite (au-delà de `inference.resilience.max_unavailable_s`). **Jamais d'échec silencieux ni de spin.** Panneau d'état : `GET /api/resources/status` + `dashboard_status.html`.
- **Allocator** : une phase servie à distance ne réserve **aucune** VRAM locale (`WorkflowRunner._phase_runs_remotely`).
- **Banc E2E** : `tests/test_e2e_workflow.py --remote-stt URL [--remote-inference URL]` ; smoke réel `scripts/smoke_remote_stt.py`.

### Pipeline STT — deux modes de chunking

**Mode pyannote_turns (prioritaire) :** si `speaker_turns.json` contient `exclusive_turns` (produit par la phase summary), `Transcriber.transcribe()` charge l'audio en mémoire une seule fois, découpe par tours pyannote, et passe des `np.ndarray` directement au backend STT actif (Cohere, Whisper ou Granite). Chaque chunk a un speaker connu ; si des timestamps mots existent, `SpeakerPunctuationRealigner` peut corriger un segment qui traverse plusieurs tours.

**Exception `audio_tres_faible` :** si le preflight détecte le flag `audio_tres_faible`, `Transcriber` force le mode 30s_fallback même si `exclusive_turns` est disponible. Sur ce type d'audio, pyannote ne détecte souvent qu'un seul tour court (~5 s), ce qui limiterait la transcription à ~17 % du signal. La cause est tracée dans `metadata/transcription_metadata.json` sous le champ `chunking_forced_30s_reason`.

**Mode 30s_fallback :** si `exclusive_turns` est absent (premier run, pyannote indisponible, ou flag `audio_tres_faible`), chunking 30s fixe suivi de `_apply_speakers()` (overlap matching). Comportement identique à l'implémentation pré-refactoring.

**Pré-traitement audio (avant STT) :** `PipelineService._run_pipeline_steps()` exécute les étapes suivantes avant la transcription finale :
0. `_run_audio_preflight()` — analyse pré-STT rapide (RMS, SNR estimé, bande passante, clipping, flags `audio_faible`/`audio_tres_faible`/`snr_faible`), sauvegarde `metadata/audio_preflight.json`. Retourne les flags utilisés par les étapes suivantes.
1. `_run_audio_scene_analysis()` — crée `AudioSceneAnalyzer(config)`, appelle `analyze(audio_path)` dans un subprocess isolé (librosa CPU), sauvegarde le résultat dans `metadata/audio_scene.json` si non vide. Retourne `{}` si désactivé, timeout ou erreur.
2. `_refresh_audio_quality_with_scene()` — réévalue `AudioQualityEvaluator` avec les données de `audio_scene.json` et met à jour `metadata/audio_quality_decision.json` si la classification change (par ex. détection de musique).
3. `_run_source_separation()` — charge `metadata/audio_analysis.json` + `metadata/audio_quality_decision.json`, appelle `SourceSeparationDecider.should_separate(analysis, quality, audio_scene=scene)`. Si séparation décidée, `SourceSeparationService.separate()` produit `vocals.wav` dans le répertoire input. La piste vocale extraite remplace `audio_path` pour la suite du pipeline.
4. `_run_audio_scene_filter()` — option désactivée par défaut (`workflow.audio_scene_filter.enabled=false`). Si activée pour le mode courant, met en silence les longues zones non vocales ciblées sans couper l'audio, produit `scene_filtered.wav`, et écrit `metadata/audio_scene_filter.json` avec `preserve_timeline=true`.
5. `_run_audio_denoise()` — option désactivée par défaut (`workflow.audio_denoise.enabled=false`). Si activé pour le mode courant et si les flags preflight correspondent (`trigger_flags`), applique un filtre ffmpeg `afftdn`, produit `denoised.wav`, et écrit `metadata/audio_denoise.json` avec `preserve_timeline=true`.
6. `_run_audio_normalization()` — option désactivée par défaut (`workflow.audio_normalization.enabled=false`). Si activée pour le mode courant, applique des filtres ffmpeg simples (`loudnorm`, high-pass optionnel), produit `normalized.wav`, et écrit `metadata/audio_normalization.json` avec `preserve_timeline=true`. Inclut aussi le traitement `weak_voice` si le preflight détecte `audio_faible`/`audio_tres_faible` et que la normalisation n'est pas déjà active : applique un gain puis loudnorm.

Ces étapes s'exécutent dans cet ordre, avant `Transcriber.transcribe()`. Le subprocess librosa se termine avant le chargement GPU pyannote/Whisper : pas de conflit de ressources. Ne jamais remplacer `audio_scene_filter` par une coupe d'audio sans remapper explicitement les timestamps.

**Qualification du son (SQUIM / DNSMOS, dans le preflight) :** `AudioPreflightAnalyzer._augment_with_squim()` ajoute, quand activé (`workflow.audio_preflight.squim`/`dnsmos`), les scores SQUIM (`squim_global` : STOI/PESQ/SI-SDR) et DNSMOS (`dnsmos_global` : SIG/BAK/OVRL), plus une `difficulty_map` lazy par fenêtre. Contraintes à respecter (cf. `docs/STT_ADAPTATIF_ET_HYBRIDE.md`, `docs/CONFIG_REFERENCE.md`) :
- **SQUIM `score_global` est borné** : il échantillonne quelques fenêtres réparties (`probes × window_s`, défaut 5 × 10 s) puis moyenne. Ne jamais lui passer le signal entier — sur un fichier long c'est une allocation démesurée → OOM, et SQUIM est de toute façon conçu pour des extraits courts.
- **DNSMOS est indépendant de SQUIM** : calculé en premier (sondes bornées ≤ 5 × 9 s), il reste disponible même si SQUIM échoue. Ne pas le re-coupler à la réussite de SQUIM. DNSMOS est ONNX **CPU-only par conception** (`CPUExecutionProvider`, modèle minuscule).
- **Choix du GPU (`squim.device="auto"`)** : `squim_scorer.pick_device()` sélectionne, en lecture seule (`torch.cuda.mem_get_info`), le **GPU le plus libre** ayant ≥ `squim.vram_mb` (défaut 5000 Mo), sinon CPU. Sur la machine multi-GPU dont le GPU 0 est saturé par le LLM d'arbitrage, SQUIM est ainsi placé sur un GPU libre (ex. `cuda:7`) **sans jamais évincer le LLM**. Ne pas utiliser `VRAMManager.ensure_free()` ici : son `_free_memory()` tue les process GPU > 4 Go (risque de tuer le LLM). Un index explicite (`cuda:2`) est respecté.
- **Repli CPU collant** : si un lot SQUIM OOM sur GPU, `score_segments` bascule CPU **une seule fois** pour tout le reste de l'appel (plus de tentative CUDA par lot). En repli CPU, le preflight élargit le pas de la frise (`squim.hop_s_cpu`, défaut 5.0 vs `hop_s` 2.5 sur GPU) pour privilégier la vitesse — la frise par fenêtre CPU étant l'étape la plus coûteuse du preflight.
- **Libération VRAM** : `squim_scorer.release_cuda_cache()` est appelée dans un `finally` à la fin de la qualification (tous chemins de sortie). Elle rend le cache d'activations CUDA réservé (l'allocateur torch ne le libère pas seul) **sans décharger le modèle** singleton (réutilisable).
- **Concurrence** : le modèle SQUIM est un singleton torch partagé ; ses inférences sont sérialisées par un verrou interne (`squim_scorer._INFER_LOCK`) car le preflight tourne hors sérialisation de l'allocateur GPU (plusieurs jobs simultanés). DNSMOS (onnxruntime) est thread-safe.
- **Résumé persisté** : `JobService.analyze()` écrit un résumé audio **compact** dans `jobs.extra_data_json["audio_summary"]` (`risk_level`, `flags`, `duration_s`, `snr_db`, `squim`, `dnsmos`, `difficulty` agrégé — **sans** la frise par fenêtre, qui reste dans `metadata/audio_preflight.json`). But : requêter/échantillonner à travers les jobs pour calibrer l'arbitrage STT (première brique d'un corpus difficulté ↔ moteur ↔ qualité, cf. `docs/STT_ADAPTATIF_ET_HYBRIDE.md`).

**VAD Silero :** `SummaryGenerator` utilise `SileroVAD` (via `faster_whisper`) pour ne soumettre au backend STT configuré que les zones de parole détectées en phase résumé (`workflow.vad.enabled_summary=true`). `AdaptiveVADConfig` ajuste les seuils depuis `metadata/audio_quality_decision.json` si `workflow.vad.adaptive=true`. La transcription finale a le VAD désactivé par défaut (`workflow.vad.enabled_final=false`) car les tours pyannote servent déjà de VAD implicite et le VAD final dégrade la qualité sur parole faible/chuchotée. Fallback transparent si `faster_whisper` est indisponible (chunking 30s).

**VAD final auto sur audio dégradé :** désactivé par défaut (`workflow.vad.auto_enable_final_on_degraded=false`) car le VAD supprime trop de parole réelle. Voir `docs/VAD_OR_NOT.md` pour l'analyse complète.

**VAD interne Whisper :** `whisper.vad_filter=false` par défaut. Le VAD interne de faster-whisper est trop agressif pour la parole française en condition réelle (pertes d'audio observées). Whisper a déjà `no_speech_threshold`, `compression_ratio_threshold` et `log_prob_threshold` pour filtrer les segments non-parole en aval.


**Whisper qualité / audio dégradé :** le backend normal reste `models.stt_backend` (`cohere`). `PipelineService._config_for_mode()` ne force un backend alternatif que si `workflow.quality_transcription.force_stt_backend` est explicitement configuré et que le mode ou le diagnostic audio correspond aux règles. Whisper active les timestamps mot-à-mot, les seuils anti-hallucination faster-whisper, `anti_hallucination.py`, l'alignement CTC optionnel (`whisper.forced_alignment.enabled=false` par défaut) et le réalignement locuteur/ponctuation.

**Biasing lexique STT expérimental :** si `whisper.lexicon_hotwords.enabled=true` et que le backend effectif est `whisper`, `PipelineService._inject_whisper_lexicon_hotwords()` lit `context/session_lexicon.json`, injecte seulement les priorités configurées (défaut `critique`/`importante`) dans `whisper.hotwords`, sauvegarde `metadata/whisper_hotwords.json` et logue candidats/injectés/exclus. Si `cohere.lexicon_biasing.enabled=true` et que le backend effectif est `cohere`, `PipelineService._inject_cohere_lexicon_biasing()` sélectionne les formes cibles validées, sauvegarde `metadata/cohere_lexicon_biasing.json`, puis `CohereTranscriber` construit un `TrieContextualBiasProcessor` au chargement du tokenizer (`start_boost` faible pour amorcer un terme, `boost` pour le compléter). Les deux options sont désactivées par défaut.

**Anti-hallucination STT :** `CohereTranscriber`, `WhisperTranscriber` et `GraniteTranscriber` appliquent `collapse_repetition_loops` depuis `anti_hallucination.py` avec leurs sections de config respectives (`cohere`, `whisper`, `granite`). Les backends partagent la même logique de détection/réduction des boucles répétitives.

**Granite expérimental :** `models.stt_backend=granite` active IBM Granite Speech 4.1 2B normal. La diarisation reste pyannote ; Granite normal est utilisé comme ASR texte pur. `granite.fix_mistral_regex=true` est passé à `AutoProcessor` quand supporté, avec fallback logué si la version `transformers` locale ne connaît pas encore ce paramètre. Les métadonnées sont écrites dans `metadata/granite.json`.

**Limitation Granite sur audio dégradé :** `PipelineService._config_for_mode()` bascule automatiquement de Granite vers le backend de production configuré dans `self.config` (ou `cohere` si celui-ci est aussi `granite`) quand `audio_quality_decision.json` indique `level=degrade` ou que `audio_preflight.json` contient le flag `audio_tres_faible`. Granite est expérimental et peu fiable sur ces types d'audio. Le backend de fallback effectivement utilisé est tracé dans le log (`Granite exclu pour audio dégradé (job=...), fallback → ...`).

**Nettoyage post-STT (`transcription_cleanup`) :** si `workflow.transcription_cleanup.enabled=true` (défaut), `Transcriber._cleanup_transcription_segments()` supprime les artefacts de sous-titrage (watermarks de diffusion : `"Sous-titrage ST' 501"`, `"FR 2021"`, `"Société Radio-Canada"`, etc.) et fusionne les micro-segments courts adjacents (même locuteur, gap court, texte bref). Les patterns d'artefacts sont configurables via `workflow.transcription_cleanup.subtitle_artifact_patterns` (liste de regex, défaut `[]` = utiliser les patterns intégrés) et `subtitle_artifact_words` (liste de phrases, défaut `[]` = utiliser les mots-clés intégrés). Les paramètres de fusion sont `merge_short_segments`, `short_segment_max_s` (défaut 0.45), `short_segment_max_words` (défaut 2), `merge_gap_s` (défaut 0.5), `merge_max_chars` (défaut 220), `remove_subtitle_artifacts` (défaut true).

**Normalisation auto loudnorm :** si l'audio a un RMS < `workflow.audio_normalization.auto_loudnorm_rms_threshold` (défaut `0.02`) et que la normalisation n'est pas déjà activée, `PipelineService._run_audio_normalization()` force automatiquement un filtre `loudnorm=I=-23:TP=-2:LRA=11`. Ce mécanisme empêche Silero VAD de rejeter un audio trop silencieux (voix chuchotée, micro lointain). Le forçage est tracé dans `metadata/audio_normalization.json` avec `"forced": true`.

**Checks qualité `suspect_no_speech_prob` et `suspect_low_word_confidence` :** `QualityReporter` détecte les segments suspects via deux checks configurables : `quality.thresholds.no_speech_prob_threshold` (défaut `0.5`, signale les segments avec `no_speech_prob` au-dessus du seuil) et `quality.thresholds.low_word_confidence_ratio` + `low_word_confidence_min` (défaut `0.5` et `0.4`, signale quand >50% des mots d'un segment ont une probabilité < 0.4). Le score composite hallucination combine ces deux signaux pour distinguer les vrais segments suspects des faux positifs (jingle, micro-fragment de diarisation).

**Analyse de scène audio (`AudioSceneAnalyzer`) :**
- Entrée : chemin audio + config `workflow.audio_scene` (seuils, timeout, detect_gender)
- Pipeline subprocess : énergie RMS → classification spectrale (flatness/ZCR → speech/music/noise) → estimation genre YIN (pitch)
- **Garde bande étroite (anti-faux-positif musique) :** `_classify_scene_frames` calcule la bande passante médiane (rolloff 95 % sur trames actives, via la fonction pure `_median_active_rolloff`). Si elle est inférieure à `workflow.audio_scene.thresholds.music_suppress_bandwidth_hz` (défaut `3000`, `0` = désactivé), la classe `music` est neutralisée : la parole compressée en bande étroite (téléphone/visio) a une flatness basse et serait sinon classée « musique » à tort, ce qui plombait le score qualité et générait de fausses `problem_segments`. La vraie musique garde un rolloff élevé et n'est pas affectée.
- Sortie JSON : `{has_music, has_noise, speech_ratio, music_ratio, noise_ratio, no_energy_ratio, non_speech_ratio, gender: {has_gender_data, dominant, male_ratio, female_ratio}, stats: {labels, total_duration_s}, scene_segments: [{label, start, end, duration_s}], problem_segments: [{label, start, end, duration_s}], gender_segments: [{start, end, label}]}`
  - `gender_segments` : liste des intervalles horodatés classés `"male"` ou `"female"` uniquement, utilisés par `_inject_speaker_genders` pour croiser avec les tours pyannote.
  - `problem_segments` : longues zones non vocales (`music`, `noise`, `noEnergy`) exposées pour diagnostic qualité sans changer automatiquement le pipeline.
- `SourceSeparationDecider.should_separate(analysis, quality, audio_scene)` : si `audio_scene.has_music=True` → séparation forcée **sauf si** `speech_ratio < scene_music_min_speech_ratio_for_force` (défaut 0.08, paramètre configurable), auquel cas la musique est ignorée comme faux positif sur parole quasi absente. Si `audio_scene=None` → logique score seule.
- `WorkflowRunner._build_gender_section(audio_scene)` : méthode statique qui génère les lignes Markdown de distribution H/F pour `diarization_context.md` (visible par la LLM de résumé). Vide si `has_gender_data=False`.
- `WorkflowRunner._assign_speaker_genders(gender_segments, turns, min_overlap_s=1.0)` : méthode statique pure. Croise les segments genre avec les tours pyannote (format flat `{speaker, start, end}`). Retourne `{speaker_id: {gender, male_s, female_s}}`. Attribue uniquement si chevauchement total ≥ `min_overlap_s` ET l'un des deux sexes domine.
- `WorkflowRunner._inject_speaker_genders(fs, audio_scene)` : lit `speakers/speaker_turns.json` sur disque, appelle `_assign_speaker_genders`, met à jour `speaker_stats.json` (ne jamais écraser `gender` déjà renseigné). Appelée depuis `_run_pyannote_after_transcription` et `run_diarization`. **Prérequis timing** : `speaker_turns.json` et `audio_scene.json` doivent exister avant l'appel — `run_diarization` est toujours appelé après `_run_audio_scene_analysis` dans PipelineService, ce qui garantit cet ordre.
- UI : bannière genre global dans l'étape Participants (si `audio_scene.gender.has_gender_data`), select genre par locuteur (Non déterminé/Féminin/Masculin). Le genre est persisté dans `speaker_stats.json` via `SpeakerDetector.save_mapping()` (champ `gender`). Pré-rempli automatiquement par `_inject_speaker_genders` si l'attribution acoustique réussit.

**Checkpoints pyannote :** `DiarizerService` réutilise `speakers/speaker_turns.json` si `speakers/diarization_checkpoint.json` correspond au même modèle, à la même empreinte audio, aux mêmes contraintes locuteurs (`min_speakers`/`max_speakers`/`num_speakers`) et aux mêmes `diarization.pipeline_params`. `speakers/speaker_embeddings.json` stocke un checkpoint acoustique simple par locuteur. Ne pas supprimer ces fichiers sans mettre à jour `docs/DATA_MODEL.md`.

**Chunking pyannote + Cohere :** le réglage production documenté est `workflow.pyannote_chunking.max_chunk_s=45` avec `cohere.chunk_length_s=30`. Ce n'est pas contradictoire : `max_chunk_s` borne les tours pyannote envoyés au backend, puis `CohereTranscriber` peut redécouper en chunks internes de 30 s. Le bench de référence réunion 2026-06 valide ce couple (`45/30`) pour un gain de vitesse sans perte texte/locuteurs. Ne pas passer Cohere à 35 s par défaut sans bench dédié `45/35` ou `35/35`.

**Diarisation pyannote — nombre de locuteurs :** sur les fenêtres de référence réunion dense 2026-06, les seuils VBx testés (`clustering.threshold=0.50/0.55/0.65`) n'ont pas amélioré le comptage en mode nombre inconnu. Le seul résultat parfait mesuré est `diarization.num_speakers=N` quand le nombre exact est connu. `min_speakers`/`max_speakers` peuvent cadrer pyannote mais ne remplacent pas une indication utilisateur fiable sur les cas multi-participants difficiles.

**Fourchette de locuteurs par job (saisie utilisateur) :** l'étape Résumé du wizard expose un champ optionnel min/max locuteurs, persisté dans `jobs.extra_data_json["speaker_hint"]` (`{min, max}`) via `POST /api/jobs/<id>/speaker-hint` (helper `_normalize_speaker_hint`, bornes 1..50, inversion tolérée). `diarizer_factory.apply_speaker_hint(config, hint)` (fonction pure) applique ce hint : il écrit `diarization.min_speakers`/`max_speakers`, pose `num_speakers` si `min == max` (comptage exact), et **bascule le backend de `sortformer` vers `pyannote` si la borne haute saisie dépasse `SORTFORMER_MAX_SPEAKERS=4`** (uniquement sur un choix explicite, jamais sur le `max_speakers` global par défaut). Le hint est injecté de façon **identique** aux deux points d'entrée diarisation (`run_speaker_detection` pour le résumé et la détection manuelle, `run_diarization` pour le pipeline final) afin que les paramètres restent cohérents entre phases et n'invalident pas le checkpoint pyannote.

**`CohereTranscriber.transcribe()` accepte deux formes d'entrée :**
- `transcribe(audio_path=Path(...))` — charge l'audio depuis le disque (usage standard)
- `transcribe(audio_path=None, audio_array=np.ndarray, sample_rate=16000)` — audio déjà en mémoire (chunking par tours, évite les I/O)

### Workflow (9 étapes affichées)
Le wizard guide l'utilisateur de l'upload au package ZIP. Chaque étape correspond à un `JobState`. Les transitions passent obligatoirement par `workflow/transitions.py`. Voir `docs/DATA_MODEL.md` pour le détail des états.

**Contrat d'état pendant le résumé :** `run_summary()` reste en `SUMMARY_RUNNING` du début jusqu'à `SUMMARY_DONE`. Sa sous-phase de diarisation appelle `run_speaker_detection(..., update_state=False)` : elle **ne doit pas** publier `SPEAKER_DETECTION_RUNNING`/`DONE` (états « en avant » du wizard, classés étape Participants), sinon `compute_statuses()` marquerait `summary=DONE` et le template afficherait un cadre « Contexte » vide avant que `meeting_context.json` ne soit écrit. La diarisation y est best-effort (un échec n'écrase pas l'état, le résumé poursuit). Les états `SPEAKER_DETECTION_*` ne sont publiés que par la détection manuelle (`POST /api/jobs/<id>/speakers/detect`), après `SUMMARY_DONE`.

**Garde anti-concurrence des phases synchrones :** `api_summary` et `api_speakers_detect` exécutent leur pipeline dans le thread HTTP et publient un état `RUNNING` pour toute leur durée. Ils renvoient `409` si le job est déjà `SUMMARY_RUNNING` / `SPEAKER_DETECTION_RUNNING` (anti double-lancement GPU et course sur `meeting_context.json`). `api_process` garde déjà la file via `can_start_processing` + `is_execution_active`.

### Modèle service/worker
`/api/jobs/<id>/process` planifie le traitement ; `JobExecutorService` l'exécute en arrière-plan. Par défaut, `workflow.queue.enabled=true` crée une entrée `job_queue` persistante et `QueueScheduler` dispatch les jobs selon priorité, calendrier et capacité (`workflow.execution.max_concurrent_jobs`, défaut 1). Supervision : `/health`, `/ready`, `/metrics`, `/api/queue/status`.

**Montée en charge (Phase B, PostgreSQL requis)** : un **rôle** sépare le tier HTTP de l'orchestrateur — `runtime.role`/`TRANSCRIA_ROLE` ∈ `all` (défaut, tout-en-un) | `web` (gunicorn -w N, n'exécute pas la file) | `scheduler` (process unique qui draine la file). Garde-fous : claim de job atomique (`QueueStore.claim`, `FOR UPDATE SKIP LOCKED`), **ordonnanceur unique** par verrou consultatif PG (`scheduler_lock.py`), réveil optionnel `LISTEN/NOTIFY` (`workflow.queue.use_listen_notify`, sinon polling), **failover actif/passif** des nœuds de ressources (`inference.nodes`). Détail : `docs/CONCURRENCE_ET_CHARGE_PHASE_B.md`. Ne jamais lancer le tier `web` avec un contexte GPU : le GPU reste dans l'orchestrateur/le nœud.

**Notifications email** : `JobExecutorService._run_process()` appelle `_notify(config, job, event, error)` juste après chaque `JobStore.update_state(COMPLETED)` ou `JobStore.update_state(FAILED)`. `_notify` délègue à `send_job_notification_async()` (module `transcria/notifications/mailer.py`) qui envoie l'email en daemon thread — jamais bloquant, absorbe toute exception. La configuration SMTP est dans `notifications.email` (`enabled`, `smtp_host`, `smtp_port`, `use_starttls`, `use_ssl`, `from_address`, `base_url`). Si `enabled=false` ou si l'adresse email de l'utilisateur est vide, aucune notification n'est envoyée.

Les états de file restent dans `job_queue.status` (`waiting`, `paused`, `running`, `done`, `failed`, `cancelled`) et dans `extra_data.execution.status`; ne pas ajouter d'états `QUEUED` ou `WAITING_RESOURCES` à `JobState` sans revoir `WorkflowState`, `WORKFLOW_STEPS` et la documentation. En mode queue, `PipelineService.run_process(..., finalize_job_state=False)` laisse `JobExecutorService` publier les états terminaux dans l'ordre `job_queue` → `extra_data.execution` → `jobs.state`; ne pas remettre un `JobState.COMPLETED` direct dans le pipeline queue, cela recrée une course visible par l'API. Les routes sensibles de file/calendrier doivent être auditées via `audit_log()`.

`transcria/queue/allocator.py` est le point de coordination GPU multi-job. Le scheduler ne réserve pas la VRAM à la place du pipeline : son admission (`_resources_available`, B6.3) vérifie le coût VRAM **local** maximal du profil (hors phases servies à distance) et, si le nœud est distant, la VRAM libre distante via `/capabilities`. Les réservations effectives se font au moment des phases dans `WorkflowRunner`/`PipelineService` via `GPUAllocator` et `GPUSession`.

Calendrier : `/admin/schedule` et `/api/schedule/windows` gèrent la table `scheduling_windows`. Règles supportées : `pause_queue`, `limit_concurrency`, `force_gpu`, `none`. `pause_queue` et `force_gpu` sont on/off ; `limit_concurrency` utilise `action_params.max_concurrent_jobs`. Ne pas ajouter de saisie "nombre de GPUs" au calendrier : avec la LLM d'arbitrage multi-GPU, seule la mesure runtime de `GPUAllocator` est fiable. `force_gpu` ne peut tuer que des processus correspondant aux `workflow.scheduling.kill_patterns` configurés, dans une fenêtre active.

Nettoyage E2E : `/admin/queue` expose aux admins globaux un bouton `Nettoyer E2E` qui supprime uniquement les jobs dont le titre commence par `E2E workflow`, leur entrée de file et leur dossier disque. Les jobs en cours sont ignorés et l'action doit rester auditée (`job_test_purge`).

### Audit de sécurité (PSSI/RGPD)
Toutes les actions sensibles des utilisateurs sont journalisées dans la table `audit_logs` via `AuditStore.log()`. Le décorateur `audit_log()` dans `audit/decorator.py` capture automatiquement `current_user`, l'adresse IP (`X-Forwarded-For` ou `request.remote_addr`) et le User-Agent. La rétention est configurable via `security.audit_retention_days` (défaut 1095 jours) et `security.audit_retention_by_family` (auth, job, lexicon, voice, config, other). La purge est exécutée automatiquement à chaque accès à la page d'accueil. Les entrées d'audit ne sont jamais supprimables par l'interface (pas de route DELETE). L'export CSV est disponible dans `/admin/audit` pour le DPO/responsable PSSI et doit journaliser `audit_export`. Toute nouvelle route sensible doit appeler `audit_log()` ou `@audit_action`.

Les routes qui servent des artefacts job ou déplacent des données hors application doivent être auditées : SRT/ZIP/audio complet, extraits audio, clips locuteurs et push SRT Editor. Les détails d'audit doivent rester des métadonnées techniques (format, durée, destination, identifiants) sans citation transcript, contenu SRT, titre de job supprimé ou chemin fichier complet.

Les lexiques peuvent contenir des noms propres. Les actions `lexicon_term_add`, `lexicon_term_modify`, `lexicon_term_delete`, `lexicon_import`, `lexicon_export`, `lexicon_scope_change`, `lexicon_job_assign` et `job_lexicon_save` doivent journaliser uniquement des métadonnées : compteurs, catégories, priorités, groupe/job, source et signaux `contains_probable_person_names`. L'export CSV lexique doit rester une route `POST`; `security.lexicon_export_admin_only=true` le réserve aux admins globaux. Ne jamais écrire les termes, variantes ou commentaires en clair dans `audit_logs.details_json` ni dans les logs applicatifs.

### Groupes utilisateurs et visibilité des jobs
Les jobs restent propriétaires d'un utilisateur (`Job.owner_id`). Les groupes (`Group`, `GroupMembership`) ajoutent une visibilité croisée : un membre voit les jobs des autres membres des groupes auxquels il appartient. Cette règle est centralisée dans `JobStore.list_for_user()` pour la liste et `_can_access_job()` / `_require_job_access()` pour les pages et API job.

Les admins globaux (`Role.ADMIN`) créent, renomment et suppriment les groupes. Les `group_admin` peuvent gérer les membres existants de leurs groupes, mais ne créent pas d'utilisateurs. Un admin de groupe ne peut pas se retirer lui-même ni laisser son groupe sans aucun admin de groupe.

### Gestion des mots de passe
Les utilisateurs authentifiés changent leur propre mot de passe via `/account/password`. La route vérifie le mot de passe actuel, la confirmation et une longueur minimale de 8 caractères. Le reset en cas d'oubli passe par l'admin global dans `/admin/users/<id>/edit`; ne pas ajouter de reset email sans configuration SMTP, tokens expirables et protections anti-abus documentées.
Si `UserStore.ensure_admin()` crée le premier admin avec `admin-change-me`, `CHANGE-ME` ou un mot de passe vide, un warning doit être logué.

### Pré-remplissage des rôles participants (LLM → section 5)
La phase summary (LLM d'arbitrage) déduit les rôles de chaque SPEAKER_XX depuis la transcription. Le flux :
1. `OpenCodeRunner._parse_structured_summary()` extrait `speaker_roles` (`{"SPEAKER_00": {"label": "Alice", "role": "..."}, ...}`)
2. `WorkflowRunner._apply_llm_suggestions()` stocke ces rôles dans `meeting_context.json["speaker_roles_llm"]`
3. `WorkflowRunner._apply_speaker_roles()` est appelé **après** la création du mapping SPEAKER_XX → participant, soit :
   - Dans le test E2E : à l'étape 7 (mapping), après `SpeakerDetector.save_mapping()`
   - En production : dans `api_speakers_map` (endpoint `/api/jobs/<id>/speakers/map`), après `SpeakerDetector.save_mapping()`
4. Le résultat est écrit dans `context/participants.json["role"]` pour chaque participant

**Important :** `_apply_speaker_roles()` nécessite que `speakers/speaker_mapping.json` existe déjà (lien SPEAKER_XX → participant_id). Ne pas l'appeler avant la création du mapping.

Le parser accepte deux formats pour les participants probables :
- `SPEAKER_XX [Fonction A] : rôle détaillé`
- `SPEAKER_XX : Fonction A — rôle détaillé`

Le format avec crochets est le format cible du prompt. Ne pas hardcoder de métiers réels ou de domaines réels dans ces exemples ; utiliser des placeholders neutres (`Fonction A`, `Rôle A`, `Organisation A`).

### Lexiques centralisés (admin / admin groupe → section 6)
Les lexiques centralisés sont gérés depuis `/admin/lexicons` par les admins globaux et les admins de groupe. Ils sont stockés en base (`group_lexicons`, `group_lexicon_entries`) et ne remplacent jamais le fichier de session validé par l'utilisateur.

Règles de périmètre :
- Un admin global peut créer un lexique global ou de groupe.
- Un admin de groupe ne peut créer/modifier que les lexiques des groupes qu'il administre.
- Le pré-remplissage d'un job utilise les lexiques du propriétaire du job et les lexiques globaux, même si le job est consulté par un admin ou par un membre d'un autre groupe.
- Si `context/session_lexicon.json` existe déjà et contient des entrées, il reste l'autorité UI ; les lexiques centraux disponibles sont seulement affichés comme contexte.

Flux étape 6 :
1. `web.routes._central_lexicon_context()` charge les lexiques accessibles au job.
2. `context/selected_lexicons.json` limite les lexiques cochés pour ce job. Si le fichier est absent, tous les lexiques accessibles sont sélectionnés.
3. `prefilter_lexicon_entries_for_display()` réduit les entrées centrales affichées : terme/variante détecté dans le transcript ou résumé, priorité `critique`/`importante`, limite douce d'affichage. Chaque entrée gardée reçoit `_display_reason` pour l'explication UI.
4. `merge_lexicon_entries()` fusionne central filtré + termes suspects LLM ; une session existante garde la priorité.
5. `LexiconManager.save()` conserve les métadonnées `source`, `central_entry_id`, `central_lexicon_id`, `central_lexicon_name`, `_display_reason`.
6. `WorkflowRunner.run_correction()` écrit `context/session_lexicon_filtered.json` et transmet ce fichier filtré à la LLM : termes présents par forme/variante + priorités `critique`/`importante` conservées en préservation.

Ne pas envoyer un lexique central complet et volumineux à la LLM sans filtrage par SRT : cela augmente le bruit et le risque de correction hors contexte.
Ne pas écraser `session_lexicon.json` quand l'utilisateur change seulement les cases de lexiques : `/api/jobs/<id>/selected-lexicons` ne sauvegarde que la sélection et recharge le préremplissage.
Les fiches `/admin/lexicons/<id>` affichent les statistiques d'usage et les contrôles qualité calculés par `CentralLexiconStore.usage_stats()` et `quality_issues()` ; ces signaux restent informatifs et ne bloquent pas la sauvegarde.

### Récupération des opencode orphelins au démarrage
`job_executor._kill_orphaned_opencode(job_id, jobs_dir, sl)` tue les processus opencode de TranscrIA laissés vivants après un redémarrage brutal. Il lit les fichiers `.opencode.pid` dans `jobs/<id>/` (écrits par `OpenCodeRunner.run()`). La réconciliation est appelée automatiquement par `init_job_executor()` au démarrage du service.

### Config singleton
`get_config()` retourne un singleton chargé une fois au démarrage. `set_config()` le met à jour en mémoire. `save_config()` écrit sur disque. Les modules qui capturent `get_config()` au démarrage ne voient pas les mises à jour ultérieures.

### Installation et bootstrap
`install.sh` orchestre l'installation complète. `scripts/bootstrap_config.py` génère `config.yaml` en fusionnant `config.example.yaml` avec les valeurs auto-détectées (`SystemDetector` : GPUs, binaires, chemins). Le fichier `.env` porte les secrets (`TRANSCRIA_SECRET`, `HF_TOKEN`).

## Pièges connus

### Sentinelle `_apply_llm_suggestions` — comparaison exacte uniquement
`WorkflowRunner._apply_llm_suggestions()` (runner.py) garde un test d'early return pour détecter un résumé indisponible. Ce test est intentionnellement une **comparaison exacte** :
```python
if not summary_text or summary_text.strip() == "Résumé indisponible.":
```
Ne jamais le remplacer par `"indisponible" in summary_text.lower()` : un résumé valide peut contenir ce mot dans son corps (ex : "fallback quand X est indisponible"), ce qui causerait un faux positif silencieux — `meeting_context.json` resterait non mis à jour sans aucun log d'erreur. La sentinelle `"Résumé indisponible."` est la seule valeur retournée par `run_summary()` quand opencode ne produit rien.

### opencode — provider `local` requis dans `~/.config/opencode/opencode.json`
`OpenCodeRunner` invoque opencode avec `--model <provider>/<model>` depuis `workflow.summary_llm.model_id` ou `workflow.arbitration_llm.model_id` (exemple : `local/qwen3-35b-arbitrage`). Dans opencode, le préfixe `local/` désigne un provider nommé `local`. Ce provider **doit** être déclaré dans `~/.config/opencode/opencode.json` pointant sur le serveur llama.cpp (port 8080 par défaut, ou `NODE_IP:8080` en topologie distribuée). Sans cette entrée, opencode ne sait pas résoudre `local/` → les appels LLM échouent silencieusement et `summary.md` conserve le placeholder.

**Ne pas écrire ce fichier à la main** : utiliser `venv/bin/python scripts/setup_opencode.py` (idempotent, format correct, ne casse pas une config existante). Helper sous-jacent : `transcria/gpu/opencode_setup.py` (`find_opencode_binary`, `ensure_local_provider`). Format **courant** produit (≠ ancien `providers`/`type:openai`/`validate`) :
```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "local": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "LLM d'arbitrage (local)",
      "options": { "baseURL": "http://127.0.0.1:8080/v1", "apiKey": "dummy-key", "timeout": 9999999 },
      "models": { "qwen3-35b-arbitrage": { "name": "LLM d'arbitrage (local)" } }
    }
  }
}
```
Le nom court (`qwen3-35b-arbitrage`, clé de `models`) peut différer de l'alias complet rapporté par llama-server (`qwen3-35b-arbitrage-ud-q8_k_xl`) : llama.cpp ignore le `model` de la requête et sert le modèle chargé.

### `correction_prompt.txt` — version courante : v2.2

**v2.2 (2026-06) — application du lexique par la LLM + délégation @general obligatoire** : la fausse prémisse « les variantes du lexique sont déjà appliquées par le système » est supprimée (aucune étape système ne les applique ; seuls les **locuteurs** le sont). La LLM est désormais responsable d'**appliquer le lexique validé en contexte** (section 2bis) : pour chaque entrée non `_preservation_only`, remplacer une variante par la forme cible (`replace_by` sinon `term`) **uniquement** quand le contexte confirme le terme, jamais un homographe légitime — pas de substitution aveugle par script. Le rapport documente substitutions appliquées et variantes non remplacées. La délégation à des subagents **@general est obligatoire** (plages disjointes traitées **séquentiellement** pour éviter une course à l'écriture sur `transcription_corrigee.srt`).

**summary_prompt.txt v2.6 (2026-06) — délégation @general obligatoire + synthèse de qualité** : la délégation @general devient obligatoire (plages disjointes ; l'agent principal est le **seul** à écrire `summary.md` — pas de course à l'écriture — et fusionne/dédoublonne les constats des subagents, §5.3). La Synthèse n'est plus plafonnée à « 8 à 15 lignes » : elle est **proportionnée à la durée** (≈ 1 paragraphe par point d'ODJ/thème) et de haute qualité, avec un garde-fou de fidélité (jamais inventer décision, chiffre, nom ou échéance).

**summary_prompt.txt v2.0 (2026-05-19)** : restructuration complète. Points critiques pour la compatibilité parser : section `## Participants probables` (match exact), section `## Termes douteux à valider` (match `## Termes (?:suspects|douteux).*?`), format terme `**TERME** [cat] (prio) | variantes_suspectes: ... | commentaire: ... | contextes: ...`, `(aucune)` filtré par `empty_markers`, séparateur `||` pour contextes multiples (`_parse_summary_contexts`), `(non identifiable)` pour participants absents. Le parser ignore uniquement les vrais placeholders `non identifiable` ; il conserve une ligne `SPEAKER_XX [label]` si ces mots apparaissent dans le texte du rôle.

**v1.7 (2026-05-18) — vérification par sous-agent** : section 15 ajoutée. Après écriture des fichiers, un sous-agent relit le SRT corrigé et le lexique depuis le disque à froid, croise avec les corrections déclarées dans le rapport pour détecter les hallucinations (corrections déclarées mais non appliquées), corrige les variantes restantes, et documente le résultat dans `## Vérification sous-agent`. L'indépendance du sous-agent (lecture des fichiers réels, pas de mémoire de travail partagée) est le point clé.

**v1.6 (2026-05-18) — anti-split SRT** : la LLM peut, sur de longues transcriptions, écrire la première moitié du SRT corrigé dans `correction_report.md` et la seconde dans `transcription_corrigee.srt`. La v1.6 ferme cette ouverture via :
- Section SORTIES renforcée : `transcription_corrigee.srt` doit contenir la **totalité** des segments (1→N), `correction_report.md` est du Markdown pur (aucune ligne SRT tolérée).
- Checks 11 (complétude SRT) et 12 (séparation fichiers) ajoutés à la VÉRIFICATION FINALE.
- Instruction inline `run_correction()` mise à jour avec les mêmes contraintes.

**v1.5 (2026-05-18) — `mapped_name` immuable** : le modèle doit recopier le `mapped_name` verbatim, caractère par caractère, sans normalisation de casse, accent ou orthographe. Trois niveaux de défense : définition absolue (Section 1 LOCUTEURS), extraction préalable obligatoire de la table `speaker_id → mapped_name` avant tout segment (Étape B de la PREMIÈRE ACTION), vérification finale (check 10 de la VÉRIFICATION FINALE).
Cas concret qui a motivé cette v1.5 : `mapped_name = "stephen"` → la LLM corrigeait en `"Stéphane"` car "Stéphane" est prononcé dans l'audio.

### Cohere ne fait PAS de diarization
`CohereTranscriber.transcribe()` retourne `{start, end, text}` — **pas de `speaker`**. Les labels de locuteurs viennent uniquement de pyannote via `_apply_speakers()`.

### `job_context.yaml` n'est pas garanti avant toutes les phases LLM
Le résumé LLM tente de lire `context/job_context.yaml`, mais ce fichier n'est construit qu'après le mapping locuteurs et le lexique. Le code tolère un chemin absent — ne pas supposer sa présence avant ces étapes.

### Mode debug et speechbrain/k2_fsa
`server.debug: true` active le reloader Werkzeug, qui recharge les modules CUDA et provoque un crash avec `speechbrain`/`k2_fsa` (importés par pyannote). **Toujours garder `debug: false` en production.**

### `exclusive_turns` absent au premier run
Lors du tout premier job, `speaker_turns.json` n'existe pas encore quand la transcription finale tourne. `Transcriber.transcribe()` bascule automatiquement en mode 30s_fallback. C'est normal : `exclusive_turns` est produit par la phase summary (étape 3), qui précède toujours la transcription finale (étape 7).

### `CohereTranscriber` — ne pas passer `audio_path=None` sans `audio_array`
Si `audio_path=None` et `audio_array=None`, `librosa.load(None)` lèvera une exception. Toujours fournir l'un ou l'autre. Le mode `audio_array` est réservé au chunking interne — les appels externes utilisent `audio_path`.

### VAD Silero — fallback transparent et activation séparée
`SileroVAD` est utilisé en pré-transcription par `SummaryGenerator` si `workflow.vad.enabled_summary=true`. La transcription finale a le VAD désactivé par défaut (`enabled_final=false`) et l'auto-activation sur audio dégradé aussi (`auto_enable_final_on_degraded=false`). Le VAD interne de Whisper (`vad_filter`) est également désactivé par défaut. Voir `docs/VAD_OR_NOT.md` pour l'analyse complète. Si `faster_whisper` n'est pas installé, `SileroVAD.available` retourne `False` et les appelants basculent en chunking 30s fixe sans erreur.

### Qualité SRT — garde-fous déterministes
`QualityReporter` signale maintenant une charge de relecture (`review_load`) avec noms de locuteurs modifiés, segments marqués étrangers, segments non latins et segments courts suspects. Les marqueurs courts de bruit ASR sont configurables via `quality.asr_noise_markers`; ne pas ajouter de phrases métier ou de cas client dans le code pour ces heuristiques.

### tests/ couvre le métier, moins les intégrations GPU
La suite pytest dans les modules `test_*.py` (plus E2E) couvre stores, config, contexte, qualité, exports, routes Flask et workflow. Le nombre de tests varie avec les ajouts ; la plupart mockent les dépendances GPU/LLM. `test_e2e_workflow.py` requiert un vrai GPU.

### `_inject_speaker_genders` — ordre d'appel et prérequis disque
`_inject_speaker_genders(fs, audio_scene)` lit `speakers/speaker_turns.json` directement sur le filesystem du job. Elle doit donc être appelée **après** que la diarisation ait écrit ce fichier. Dans le flow résumé (`_run_pyannote_after_transcription`), ce fichier est écrit par `run_speaker_detection` juste avant — ordre garanti. Dans le pipeline qualité (`run_diarization`), ce fichier est écrit par `DiarizerService.diarize()` juste avant l'appel — ordre garanti. `audio_scene` peut être un dict vide (la méthode retourne `{}` sans erreur si `gender_segments` est absent).

### E2E : utiliser impérativement `venv/bin/python`, pas `python`
Le Python système (3.13, `/usr/bin/python`) n'a pas accès aux packages du venv (`pyannote`, `torch`, `cohere_transcriber`). Lancer `python tests/test_e2e_workflow.py` depuis le système donne « pyannote non disponible » silencieusement. Toujours utiliser `venv/bin/python tests/test_e2e_workflow.py` ou activer le venv au préalable (`source venv/bin/activate`).

## Règles absolues

1. **Toujours** vérifier `_require_job_access(job, current_user)` dans les routes API qui modifient un job.
2. **Jamais** committer `config.yaml` (contient des chemins absolus de production) ni `.env` (secrets).
3. **Toujours** passer `config: dict` en paramètre aux fonctions du moteur, jamais `get_config()` direct (sauf dans les routes).
4. **Ne pas** modifier `JobState` ou `WORKFLOW_STEPS` sans mettre à jour `WorkflowState.compute_statuses()`.
5. **Ne pas** ajouter de nouveaux fichiers runtime dans l'arborescence job ou le stockage sensible (`voices/`) sans documenter dans `DATA_MODEL.md`. Fichiers existants à ne pas supprimer sans mise à jour de `DATA_MODEL.md` : `metadata/audio_scene.json`, `metadata/audio_quality_decision.json`, `metadata/audio_normalization.json`, `metadata/audio_scene_filter.json`, `metadata/audio_preflight.json`, `metadata/audio_denoise.json`, `metadata/audio_excerpts/*.wav`, `speakers/diarization_checkpoint.json`, `speakers/speaker_embeddings.json`, `speakers/voice_matches.json`.
6. **Toujours** préserver les champs LLM dans `MeetingContextManager.save()` (la liste `llm_fields`).
7. **Toujours** garder cohérents `meeting_context.json` et `job_context.yaml/json` quand un champ alimente le LLM de correction.
8. **Toujours** protéger les endpoints système JSON avec les mêmes permissions que les pages HTML équivalentes.
9. **Toujours** passer par `workflow/transitions.py` pour la logique de lancement/annulation/reprise de traitement.
10. **Ne jamais** tuer un processus opencode par nom de processus — utiliser uniquement les fichiers `.opencode.pid` dans le répertoire du job (cf. `_kill_orphaned_opencode`). Il peut y avoir d'autres opencode sur la machine non liés à TranscrIA.
11. **Ne jamais** hardcoder un domaine métier réel dans les prompts, le code applicatif, l'UI ou les tests (exemples : organismes, sigles, produits, secteurs, métiers issus de jobs réels). TranscrIA doit rester neutre et réutilisable par tout type d'organisation. Les exemples doivent utiliser des placeholders génériques (`SIGLE_A`, `Organisation A`, `Terme métier A`, `Variante phonétique A`) et le code ne doit contenir aucun mapping métier spécifique.
12. **Toujours** appliquer la même règle d'accès groupe sur les pages et API job : si un membre peut voir un job via groupe, les endpoints job correspondants doivent passer par `_require_job_access()` ou une vérification équivalente.

## Documentation complémentaire

| Fichier | Contenu |
|---|---|
| `docs/INSTALL.md` | Guide d'installation complet (install.sh, venv, modèles, service systemd, dépannage) |
| `docs/TECHNICAL.md` | Architecture détaillée, flux de données, API REST, pipeline GPU |
| `docs/DATA_MODEL.md` | Schéma de données, états, transitions, arborescence disque |
| `docs/CONFIG_REFERENCE.md` | Référence complète des paramètres config.yaml |
| `docs/VAD_OR_NOT.md` | Analyse des systèmes VAD, tests comparatifs, recommandations par type de fichier |
| `docs/PARAKEET_STT_INTEGRATION.md` | Intégration du backend Parakeet TDT 0.6B v3 (NeMo) |
| `docs/SERVICE_RESSOURCES_GPU.md` | Inférence distante v1 : topologies frontale/ressources, autonomie VRAM du STT (A/B/C), `/capabilities`, mode dégradé |
| `docs/MIGRATION_API_SERVEUR_GPU.md` | Contrat d'API du nœud de ressources distant (implémenté ; renvois §4bis depuis `inference_service/`) |

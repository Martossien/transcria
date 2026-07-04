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

# Préflight de diagnostic (GPU-free, sans effet de bord) — à lancer après config.yaml
venv/bin/python scripts/doctor.py            # config, schéma DB, script/serveur LLM, opencode, nœuds, dossiers
venv/bin/python scripts/doctor.py --strict   # avertissements = échec (code ≠ 0)
venv/bin/python scripts/doctor.py --llm-smoke # opt-in : test RÉEL opencode→LLM→texte (LLM up + VRAM, non GPU-free)

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

# Déploiement conteneurisé (cf. docs/DOCKER.md)
scripts/setup_docker_gpu.sh          # active l'accès GPU Docker (nvidia-container-toolkit + CDI) ; --check pour vérifier
scripts/docker_quickstart.sh         # turnkey : prérequis + config/.env + build + compose up + /health (--cpu, --down)
# Entrypoint conteneur par rôle (jamais install.sh) — web|scheduler|resource-node|migrate|all
python -m transcria.deploy.entrypoint <role>

# Tests — ⚠️ TOUJOURS via le venv (python système = pas de python-docx → 21 faux échecs)
venv/bin/python -m pytest tests/ -q              # suite mockée majoritaire, pas de GPU requis
venv/bin/python -m pytest tests/test_auth.py -v

# ⚠️ PostgreSQL de test : par défaut conftest lance un Postgres éphémère via pg_ctl/initdb
#    — qui ÉCHOUE en root (« initdb erreur 1 »). En local/root, pointer vers un Postgres
#    EXISTANT (mode noproc, sans initdb) en exportant :
#      export TRANSCRIA_TEST_PG_HOST=127.0.0.1 TRANSCRIA_TEST_PG_PORT=5432 \
#             TRANSCRIA_TEST_PG_USER=postgres TRANSCRIA_TEST_PG_PASSWORD=...
#    (le serveur doit autoriser CREATE DATABASE ; chaque run crée/détruit une base jetable).

# CI (.github/workflows/tests.yml) — 3 gates, reproductibles en local :
ruff check transcria/ inference_service/ --line-length 140 --select E,W,F,I
mypy transcria/ inference_service/ --ignore-missing-imports
venv/bin/python -m pytest tests/ -q --cov=transcria --cov-fail-under=75   # seuil 75 % (actuel ~80 %)
# Tests réseau (faux serveurs sur vrai socket) : marqueur "integration" → -m integration / -m "not integration"
# ⚠️  Tests E2E : TOUJOURS utiliser le python du venv (pyannote et Cohere n'y sont que là)
venv/bin/python tests/test_e2e_workflow.py --skip-llm               # E2E rapide (1 GPU)
venv/bin/python tests/test_e2e_workflow.py                          # E2E complet (GPUs + LLM requis)
venv/bin/python tests/test_e2e_workflow.py --keep                   # Conserve le job pour inspection
venv/bin/python tests/test_e2e_workflow.py --audio tests/test2.mp3  # Autre fichier audio

# Lint / format (cf. CI ci-dessus pour les commandes exactes qui gatent)
# black n'est PAS utilisé. Respecte le style du fichier que tu modifies.
```

## Gates de vérification (rituel obligatoire avant de déclarer « fini »)

Leçons durement acquises (beta.8/beta.9), NON négociables :

```bash
# Les commandes EXACTES de la CI, sur l'ARBRE ENTIER (jamais une version ciblée) :
set -o pipefail   # OBLIGATOIRE : `mypy … | tail -1` a déjà laissé passer une CI rouge
venv/bin/python -m ruff check transcria/ inference_service/ --line-length 140 --select E,W,F,I
venv/bin/python -m mypy transcria/ inference_service/ --ignore-missing-imports
venv/bin/python -m pytest tests/ -q --cov=transcria --cov-fail-under=75
```

1. **Jamais de pipe qui masque un code de sortie** sur un gate (`cmd | tail` → l'échec
   devient invisible). `set -o pipefail` ou pas de pipe du tout.
2. **UI : piloter ET voir.** Un banc Playwright de GESTES réels (pas des GET), puis
   **revue visuelle de chaque capture** — la revue attrape ce que les assertions ratent
   (7 vrais défauts attrapés ainsi sur l'éditeur SRT en une semaine).
3. **Tests aux limites** sur tout champ nouveau : vide / 1 car. / très long / unicode
   exotique / type incorrect. Oracle : jamais de 500, message FR qui guide.
4. **E2E GPU réel** pour tout ce qui touche le pipeline (instance jetable, config de
   prod copiée en scratch, PG jetable) — le mock ne prouve rien sur les phases LLM.
5. **Instance de banc ≠ instance de prod** : port dédié (7899/7901), `TRANSCRIA_CONFIG`
   scratch, kill par `lsof -ti tcp:PORT | xargs -r kill` (JAMAIS pkill par motif).
6. Après modification de code Python : **redémarrer le serveur de banc** (les templates
   rechargent à chaud, pas les routes).

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
  install.sh                # Bootstrap + câblage d'installation (la logique métier vit dans transcria/installer)
  Dockerfile                # Image CPU multi-étages (web/scheduler/migrate) ; ENTRYPOINT = transcria.deploy.entrypoint
  Dockerfile.allinone-gpu   # Image all-in-one GPU SLIM (CUDA 12.6 ; compile llama.cpp = LLM embarquée ; NeMo/Sortformer). AUCUN poids baké → publiable (GHCR)
  Dockerfile.allinone-bundled # Idem + 3 modèles NON gated BAKÉS (whisper+Sortformer+Qwen-9B) → zéro-download/hors-ligne ; build local (~31 Go) ; /licenses/ (attributions)
  docker-compose.yml        # split: db→migrate→web+scheduler ; gpu: db→migrate-gpu→all-in-one (image GPU, CDI). TRANSCRIA_HF_SOURCE=hfcache → cache HF en volume nommé (mode bundled)
  licenses/                 # Attributions des modèles embarqués (image :bundled) : NOTICE.md + NVIDIA Open Model License + MIT faster-whisper
  .dockerignore             # Exclut venv/secrets/artefacts du contexte de build (ré-inclut licenses/)
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
      llm_profiles.py       # catalogue de profils LLM (data/llm_profiles.yaml) + select_profile piloté matériel (mono/multi) ; cf. docs/LLM_BACKENDS.md
      system_detector.py    # SystemDetector.detect() — GPUs, binaires, RAM, disque
    database.py             # db = SQLAlchemy()
    diagnostics/
      doctor.py             # Préflight GPU-free : config, schéma DB (compare_metadata), script/serveur LLM, opencode, nœuds, dossiers
    installer/              # Logique métier d'installation fondue depuis install.sh (modules testés, runner injectable)
      cli.py                # `python -m transcria.installer.cli <phase>` — 8 phases : python-env, config, config-proxy, opencode, postgres, postgres-bootstrap, systemd, summary
      console.py            # Rendu [OK]/[INFO]/[WARN]/[ERROR] fidèle au shell (ANSI auto-off hors TTY)
      python_env.py / config_phase.py / opencode_phase.py / ollama_phase.py / postgres_phase.py / systemd_phase.py / summary_phase.py
    deploy/                 # Déploiement conteneurisé (P5)
      entrypoint.py         # Entrypoint Docker par rôle (jamais install.sh) : attente DB, garde PostgreSQL, exec du serveur du rôle
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
      runner.py             # WorkflowRunner — exécution des étapes (dont run_refine : chat d'affinage post-workflow)
      refine_store.py       # RefineStore — chat d'affinage : historique refine/chat.json, demande request.json, versions/v<N>/ (snapshots restaurables), extract_proposal (label contractuel tolérant)
      refine_llm.py         # Appel LLM DIRECT du mode discuss (build_discuss_messages + chat_completion : /v1/chat/completions, thinking désactivé, filtre <think>)
      transitions.py        # logique lancement / annulation / reprise ; statuts d'exécution (queued/running/waiting_vram/terminal) + mark_execution_waiting_vram()
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
      diarization_pcm.py    # DiarizationPcmPreparer — cache WAV PCM 16 kHz mono réservé à pyannote, timeline vérifiée
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
      corpus.py               # Corpus difficulté↔qualité par segment (brique 2 calibration) : difficulty_for_range/build_segment_corpus/summarize_corpus (purs)
      transcriber_factory.py# TranscriberFactory — sélection backend selon config
      transcription.py      # Transcriber — chunking pyannote/30s + alignement + realignment + _cleanup_transcription_segments() (artefacts + micro-segments)
      base_diarizer.py      # BaseDiarizer (ABC) — interface commune + méthodes partagées (cache, clips, embeddings, fingerprint)
      diarization.py        # DiarizerService(BaseDiarizer) — backend pyannote + preload audio + batch sizes + cache PCM optionnel + hook progress logué + exclusive_speaker_diarization + pipeline_params expérimentaux + checkpoints
      sortformer_diarizer.py# SortformerDiarizer(BaseDiarizer) — NVIDIA Sortformer 4spk v2.1 expérimental (NeMo, language-agnostic, max 4 locuteurs, chargement HF ou `.nemo` local via `_find_nemo_file`)
      diarizer_factory.py   # create_diarizer(), get_diarizer_vram_mb(), list_available_backends() — sélection backend selon models.diarization_backend ; apply_speaker_hint() applique la fourchette de locuteurs du job (+ guard Sortformer ≤ 4)
      remote_transcriber.py # RemoteTranscriber(BaseTranscriber) — STT distant (protocole OpenAI, concurrent_safe)
      remote_diarizer.py    # RemoteDiarizer(BaseDiarizer) — diarisation distante via inference_service ; transmet la fourchette de locuteurs (_effective_speaker_params → client.diarize(speaker_params) → /infer/diarize), parité local/distant
      speaker_detection.py  # SpeakerDetector
      summary.py            # SummaryGenerator — VAD pré-transcription + backend STT configuré
    context/
      meeting_context.py    # MeetingContextManager + MEETING_TYPES (18 types) + TYPE_SPECIFIC_FIELDS (champs par type)
      participants.py       # ParticipantsManager
      invite_parser.py      # sanitize_invite()/render_invite_markdown() — brief d'invitation collé (noms via e-mails, contexte), e-mails retirés
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
                            #   rendu du gras markdown LLM via _split_markdown_bold()/_add_markdown_runs() (Synthèse),
                            #   champs type-spécifiques, thèmes visuels par type (_DocxTheme), quorum CSE auto.
                            #   generate_docx_report(job_id, jobs_dir, output_path). Exclu de mypy (python-docx sans stubs).
    gpu/
      vram_manager.py       # VRAMManager — orchestration cycle GPU + recalage VRAM mesuré au 1er load (Ollama /api/ps)
      gpu_session.py        # GPUSession — context manager
      llm_backend.py        # LLMBackend (script/ollama/http) + cycle de vie unifié unload()/is_loaded()/measured_vram_mb() ; cf. docs/LLM_BACKENDS.md
      llm_footprint.py      # empreinte VRAM DÉRIVÉE (poids réel du fichier + KV calculé archi×contexte) — jamais de taille en dur
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
      mailer.py             # EmailConfig, build_email_config(), send_job_notification_async(), send_admin_vram_alert_async() — SMTP fire-and-forget daemon thread
      admin_alerts.py       # get_admin_emails() + alert_admin_vram_wait() — alerte ADMIN « job en attente de VRAM » (e-mail + log WARNING), best-effort
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
      ui_labels.py          # libellés FR des états de job (filtres Jinja state_label/state_badge) — JAMAIS d'état brut à l'écran
      prompt_files.py       # édition web des prompts LLM (liste FERMÉE de 3 fichiers, .bak, atomique) + scripts en LECTURE SEULE (décision sécurité)
      templates/            # base.html (navbar à menus déroulants) + templates par étape
      static/css/           # transcria.css (tokens + composants — docs/archive/REFONTE_UI.md ; pas de styles inline)
      static/js/            # wizard.js, wizard-api.js
  inference_service/        # Service Flask « nœud de ressources » (diarize/voice-embed in-process A/B/C)
    app.py                  # create_app() + garde clé API sur /infer/* et /engines/*
    engine.py / diarize_engine.py # moteurs in-process (CAS A/B/C, idle-offload)
    capabilities.py         # build_capabilities() (pur)
    routes/                 # health, capabilities, engines (/engines/ensure), voice_embed, diarize
  jobs/                     # Données runtime (1 sous-répertoire par job)
  configs/
    prompts/                # Prompts LLM (summary, correction, final_review, refine_{discuss,apply}) — placeholders abstraits, JAMAIS d'extrait réel de transcription ; summary porte {{TYPES_REUNION}}/{{INDICES_TYPES}}/{{CHAMPS_EXTRACTION_TYPE}} substitués à l'exécution
  scripts/
    bootstrap_config.py     # Génère config.yaml depuis config.example.yaml + auto-détection
    doctor.py               # Préflight GPU-free (cf. transcria/diagnostics/doctor.py) — wrapper CLI mince
    setup_docker_gpu.sh     # Active l'accès GPU Docker (nvidia-container-toolkit + spec CDI) ; --check
    docker_quickstart.sh    # Turnkey : prérequis + génération config/.env + build + compose up + /health
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
  tests/                    # modules test_*.py + E2E (mocks GPU/LLM majoritaires) — 2500+ tests
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
- **Prêt-à-traduire (discipline i18n, sans i18n)** : tout libellé d'état de job passe par
  `transcria/web/ui_labels.py` (filtres `state_label`/`state_badge`) — jamais d'état brut ni de
  libellé en dur dans un template ou du JS ; les prompts LLM restent des **fichiers**
  (`configs/prompts/`), jamais des chaînes en dur ; ne pas stocker en base des messages destinés
  à l'affichage quand une clé suffit. Objectif : pouvoir ajouter une langue plus tard **sans
  refonte** (cf. README « Language »). On ne traduit pas aujourd'hui ; on ne se l'interdit pas demain.

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

`CUDA_VISIBLE_DEVICES` est supporté : les ids physiques remontés par nvidia-smi sont remappés vers les ordinaux CUDA visibles avant de construire `cuda:N`. Si `CUDA_VISIBLE_DEVICES=-1`, aucun GPU ne doit être sélectionné. La libération VRAM ciblée doit respecter le GPU visible demandé et les patterns `workflow.scheduling.kill_patterns`; ne pas tuer des processus GPU génériques hors liste.

**Note NeMo (Parakeet) :** `ASRModel.from_pretrained()` ignore `device_map` et charge sur `cuda:0` par défaut. `ParakeetTranscriber.load()` appelle `torch.cuda.set_device()` avant le chargement pour forcer le GPU cible.

**Placement « auto » de la diarisation :** `diarization.device` accepte `"auto"`/`"cuda"` (générique) → résolu vers la **carte la plus libre ≥ VRAM requise** via `squim_scorer.pick_device`, **au moment du chargement** de pyannote (donc en contournant les cartes déjà prises par l'arbitrage/le STT). Un index explicite (`cuda:N`) est respecté tel quel ; repli CPU si rien d'éligible. Ne pas revenir au `cuda:0` figé (régression historique : OOM sur le GPU du LLM en multi-GPU). En split, mettre `diarization.device: auto` ET `models.diarization_backend: remote` (sinon la diar est traitée comme locale par `remote_requirements`).

**`ensure_arbitrage_llm_ready(expected_model_id)`** est le point d'entrée unique avant tout usage de la LLM d'arbitrage. Elle vérifie l'état réel du serveur (`/v1/models` + inférence test) et choisit parmi trois chemins logués explicitement :
- **CAS A** : LLM active et bon modèle → réutilisation directe, zéro redémarrage
- **CAS B** : LLM active mais mauvais modèle → redémarrage (warning logué)
- **CAS C** : LLM absente ou non saine → libération GPU + lancement depuis zéro

**Cycle de vie LLM** : chaque étape appelle uniquement `ensure_arbitrage_llm_ready()`. L'arrêt (`stop_arbitrage_llm()`) est fait **une seule fois** en fin de pipeline par `PipelineService._release_arbitrage_llm()`, qui vérifie d'abord `is_arbitrage_llm_running()` avant d'agir. `is_arbitrage_llm_running()` doit tester l'API OpenAI-compatible (`/v1/models` + inférence) avant tout fallback port/PID : `lsof` seul peut produire de faux négatifs sous systemd/sandbox. Ainsi la LLM reste vivante entre le résumé et la correction (CAS A garanti pour la correction si le résumé l'a démarrée).

**LLM d'arbitrage DISTANTE** (`services.arbitrage_llm_host` ≠ `127.0.0.1`/`localhost`, topologie split) : `vram_manager` honore l'hôte configuré pour TOUTES ses sondes (`_is_remote_arbitrage()`), et ne gère **PAS** le cycle de vie d'une LLM distante — il ne la lance/arrête jamais localement, il la **consomme** si saine (sinon échec explicite : c'est au nœud de la démarrer). Idem pour l'admission du scheduler (`is_arbitrage_llm_running` distant ⇒ `_llm_admissible` True, aucune VRAM locale exigée pour la LLM). Ne JAMAIS recâbler une sonde/un lancement sur `127.0.0.1` en dur (régression historique : worker CPU qui croit la LLM éteinte → boucle de dispatch différé). **Source unique** de l'hôte/port : `opencode_setup.resolve_arbitrage_endpoint` / `is_remote_arbitrage` (env > config > 127.0.0.1), partagée par `vram_manager`, `GPUAllocator` et `provision_opencode`. **Concurrence (test de charge 2026-06-23, `docs/PLAN_TEST_CHARGE.md`)** : pour une LLM **distante** (vLLM qui batche), (1) le **verrou LLM de l'allocator est no-op** (`GPUAllocator._arbitrage_remote` ; le sérialiser étranglait le débit + faisait échouer `correction`), (2) `stop_arbitrage_llm` est **no-op**, (3) le health-check `ensure_arbitrage_llm_ready` se contente de `/v1/models` (pas de test-inférence qui sature sous charge), (4) `run_correction` remonte `vram_wait` (re-queue gracieux) si la LLM distante est transitoirement indisponible — JAMAIS un échec dur. Capacité d'admission du nœud = `resource_node.max_concurrent_jobs` (annoncée dans `/capabilities`) ; les moteurs in-process sérialisés (diar/voice-embed) ne bornent pas l'admission. Sweet spot ≈ 4 sur 4×3090/27B.

**Chargement de modèles torch/transformers = sous verrou.** Tout chargement (`from_pretrained`, `Pipeline.from_pretrained`, `SQUIM_OBJECTIVE.get_model`, NeMo `ASRModel.from_pretrained`…) DOIT passer par `transcria.gpu.model_load_lock.model_load_lock()`. Raison : `transformers from_pretrained(device_map=…)` déclenche `accelerate.init_empty_weights()`, un **monkeypatch GLOBAL non thread-safe** qui place les `nn.Module` sur le device `meta` ; sous concurrence il contamine l'instanciation d'un AUTRE modèle (ex. pyannote) → `Cannot copy out of meta tensor`. Le verrou sérialise la fenêtre courte d'instanciation ; l'inférence reste concurrente. (Idem : opencode tourne avec `XDG_DATA_HOME` par invocation — db SQLite isolée — sinon deux `opencode run` concurrents se figent sur le verrou writer.)

`services.arbitrage_api_model_id` dans `config.yaml` doit correspondre à l'alias rapporté par le serveur (lancer `scripts/check_arbitrage_llm.sh` pour vérifier). `services.arbitrage_llm_port` remplace `qwen_port` pour les nouvelles configs. `services.llm_cleanup_ports` remplace `vllm_port` et liste les ports de backends LLM concurrents à libérer avant lancement. Les anciens noms restent lus par compatibilité. `free_all_gpus()` reste disponible pour les resets forcés uniquement.

Les références `qwen_*` encore présentes sont des aliases de compatibilité ancienne version ou des exemples de modèle local. Ne pas introduire de nouvelle dépendance fonctionnelle au nom Qwen : le contrat applicatif est "LLM d'arbitrage OpenAI-compatible configurée".

### Inférence distante (frontale + nœud de ressources)

TranscrIA peut tourner **tout-en-un** (ressources GPU locales, mode historique) ou en **frontale** dont les ressources GPU sont sur un **nœud distant**. Activé par la section `inference` de la config (`mode: local | remote | hybrid`). Détail complet : `docs/SERVICE_RESSOURCES_GPU.md`.

- **STT distant** : `RemoteTranscriber` (`transcria/stt/remote_transcriber.py`) parle le protocole **OpenAI** `/v1/audio/transcriptions` via `AsrClient` (`transcria/inference/asr_client.py`) — moteur de serving **non hardcodé** (vLLM, SGLang…). Sélection par `transcriber_factory._should_use_remote_stt` (mode remote/hybrid + `inference.stt.backends[<backend>].url`). `response_format` par backend (Cohere refuse `verbose_json` → `json`). Conversion WAV 16k mono systématique (l'endpoint rejette le MP3). Concurrence par tour via `inference.stt.concurrency` (>1, backends `concurrent_safe`).
- **Diarisation / empreinte vocale distantes** : `RemoteDiarizer`, `RemoteVoiceEmbeddingBackend` + service Flask `inference_service/` (routes `/infer/diarize`, `/infer/voice-embed`). Transport `inference.transport.audio` : `upload` OBLIGATOIRE en vrai distant (`file_ref` n'est valable qu'en filesystem partagé).
- **Autonomie VRAM du STT** (cycle A/B/C comme la LLM d'arbitrage, sans être intrusif) : `SttVramPlanner` (`transcria/gpu/stt_vram_planner.py`, sémantique vLLM = fraction × VRAM totale, pas la taille modèle) + `SttEngineSupervisor` (`transcria/gpu/stt_engine_supervisor.py`). L'admin décide du **placement** (manifeste `resource_node.engines`, scripts `launch_stt_*.sh`) ; le service décide du **quand** (réutilise / lance à la demande via `POST /engines/ensure` / 503 si saturé). `GET /capabilities` expose l'inventaire (GPU, VRAM, moteurs + santé).
- **Mode dégradé (admission §7.2)** : `resource_gate.prepare_remote_resources()` branché en pré-vol de `PipelineService.run_process` — nœud joignable → poursuit (+ ensure STT) ; injoignable → file (transitoire) ou échec explicite (au-delà de `inference.resilience.max_unavailable_s`). **Jamais d'échec silencieux ni de spin.** Panneau d'état : `GET /api/resources/status` + `dashboard_status.html`.
- **Allocator** : une phase servie à distance ne réserve **aucune** VRAM locale (`WorkflowRunner._phase_runs_remotely`).
- **`resource_node.engines[].gpu_mem`** est transmis au lanceur STT (`STT_GPU_MEM`, via `make_script_launcher`) → il pilote réellement `--gpu-memory-utilization` du moteur vLLM, pas seulement l'admission. Conséquence : pour un ASR léger (Cohere ~4 Go), mettre `gpu_mem` bas (ex. 0.5) — sinon vLLM réserve ~0.85×VRAM d'une carte.
- **Convention URL STT** : `inference.stt.backends[<b>].url` DOIT finir par `/v1` (l'`AsrClient` poste `{url}/audio/transcriptions` → sans `/v1` = 404 silencieux ⇒ transcript vide).
- **Banc E2E** : `tests/test_e2e_workflow.py --remote-stt URL [--remote-inference URL]` ; smoke réel `scripts/smoke_remote_stt.py`.
- **Banc split entièrement containerisé (vLLM)** : `docker-compose.split-gpu.yml` + `config.split.example.yaml` + `Dockerfile.worker`/`Dockerfile.resource-node` (images construites EN exécutant `install.sh`, le nœud ajoutant un venv vLLM isolé) + `scripts/launch_arbitrage_vllm.sh` (arbitrage Qwen3.6 : `--enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser qwen3`) + `scripts/verify_split_topology.py` (vérif E2E sur fichier son ; `--node ""`/`--arbitrage ""` sautent le plan de contrôle → le **même** script valide aussi l'all-in-one). Plan/journal détaillé et findings : `docs/PLAN_TEST_SPLIT_VLLM.md`. **Trois modes validés E2E sur images bakées** (8× RTX 3090, 2026-06-23) : all-in-one + frontale + nœud de ressources, qualité 97/100 chacun.

### Pipeline STT — deux modes de chunking

**Mode pyannote_turns (prioritaire) :** si `speaker_turns.json` contient `exclusive_turns` (produit par la phase summary), `Transcriber.transcribe()` charge l'audio en mémoire une seule fois, découpe par tours pyannote, et passe des `np.ndarray` directement au backend STT actif (Cohere, Whisper ou Granite). Chaque chunk a un speaker connu ; si des timestamps mots existent, `SpeakerPunctuationRealigner` peut corriger un segment qui traverse plusieurs tours.

**Exception `audio_tres_faible` :** si le preflight détecte le flag `audio_tres_faible`, `Transcriber` force le mode 30s_fallback même si `exclusive_turns` est disponible. Sur ce type d'audio, pyannote ne détecte souvent qu'un seul tour court (~5 s), ce qui limiterait la transcription à ~17 % du signal. La cause est tracée dans `metadata/transcription_metadata.json` sous le champ `chunking_forced_30s_reason`.

**Mode 30s_fallback :** si `exclusive_turns` est absent (premier run, pyannote indisponible, ou flag `audio_tres_faible`), chunking 30s fixe suivi de `_apply_speakers()` (overlap matching). Comportement identique à l'implémentation pré-refactoring.

**Optimisations pyannote longues réunions :** `DiarizerService.diarize()` peut préparer un cache `speakers/diarization_16k_mono.wav` via `DiarizationPcmPreparer` si `diarization.prepare_pcm_audio=true`. Ce WAV PCM 16 kHz mono est **réservé à l'inférence pyannote** ; l'audio original reste la référence pour le checkpoint fonctionnel, les clips locuteurs et les embeddings de cache. Le préparateur écrit `speakers/diarization_audio.json`, réutilise le cache par empreinte source, et refuse le WAV préparé si la durée source/cible diverge au-delà de `diarization.prepare_pcm_duration_tolerance_s` (fallback automatique sur l'original). `diarization.preload_audio=true` passe `preload=True` à pyannote pour éviter les décodages/crops répétés ; `embedding_batch_size` et `segmentation_batch_size` sont appliqués comme réglages runtime best-effort. Ne pas remplacer cette optimisation par une coupe audio : la timeline doit rester identique.

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
- **Corpus difficulté↔qualité par segment (brique 2)** : `Transcriber._write_stt_corpus()` (module pur `stt/corpus.py`) joint chaque segment transcrit à la `difficulty_map` par fenêtre (`difficulty_for_range`) et écrit `metadata/stt_corpus.json` (difficulté × moteur × confiance native × fiabilité par segment). Agrégat compact (contingence difficulté×fiabilité) promu dans `extra_data.stt_corpus_summary` (requêtable cross-jobs). Activé par défaut (`workflow.stt_corpus.enabled`), **best-effort** (n'interrompt jamais la transcription).
- **Proxy qualité du corpus (`quality_measure`)** : rempli en **phase qualité** par `WorkflowRunner._enrich_stt_corpus_quality()` — taux d'édition mot-à-mot (`segment_edit_rate`, opcodes `difflib`) entre SRT brut et SRT corrigé, apparié par **timecode** (`parse_srt_blocks`, recherche dichotomique). Tourne après correction+relecture finale (SRT définitif), best-effort, invisible (pas de step). `summarize_corpus` expose alors `edit_rate_mean` par niveau de difficulté = table de calibration. Proxy **conservateur** (la correction l'est) → exploiter en relatif. Ne pas le remplir par un script aveugle hors de ce flux ; une vérité terrain annotée pourra le remplacer (même schéma).

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

**Score qualité fondé sur la fiabilité (`compute_quality_score`, fonction pure) :** le score `/100` reflète la *fiabilité de la transcription*, pas le nombre de points à vérifier. Il combine (1) le ratio de fiabilité segmentaire (`ok` plein, `suspect` à moitié, `degrade` nul) normalisé par le nombre de segments — donc comparable quelle que soit la durée ; (2) la couverture audio, qui ne pénalise qu'en dessous du seuil ; (3) des déductions plafonnées et pondérées par gravité pour les **erreurs avérées** (noms de locuteurs altérés, hallucinations non latines, segments étrangers/vides, variantes lexique non résolues). Les signaux **contextuels** (silences, interjections brèves, chevauchements non significatifs) restent dans les points à vérifier mais ne touchent **jamais** le score — c'est ce qui empêche une réunion longue ou bruitée mais correctement transcrite de tomber à 0/100. Un segment court n'est tenu pour une probable hallucination que s'il est **corroboré** par une zone audio problématique, un `no_speech_prob` élevé ou une faible confiance des mots (sinon : interjection brève, sévérité `info`) ; les nombres dictés ne sont jamais classés bruit. Ne pas ajouter de phrases métier ou de cas client dans ces heuristiques.

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

**Brief d'invitation par job (saisie utilisateur, facultatif) :** l'étape Résumé du wizard expose un textarea où l'utilisateur peut coller une invitation (objet, corps, ordre du jour, destinataires). `context/invite_parser.sanitize_invite(raw)` (fonction pure, déterministe, générique) en tire `{brief, names}` : les **noms** sont dérivés des seules parties locales `prenom.nom` des e-mails (signal non ambigu d'une personne, qui exclut aussi les boîtes de ressource type `MS118001-201`) ; le **brief** est le texte normalisé débarrassé des adresses e-mail. **Minimisation PII** : les e-mails servent uniquement à dériver l'orthographe puis sont retirés — jamais stockés (`extra_data["meeting_invite"]` ne contient que `{brief, names}`), jamais écrits dans le brief, jamais exportés (hors liste blanche de `package_builder`). Persisté via `POST /api/jobs/<id>/meeting-invite` (`api_meeting_invite`) avant la génération du résumé ; un brief déjà enregistré est réutilisé si le champ est laissé vide. `WorkflowRunner._materialize_meeting_invite()` rend le brief en `summary/meeting_invite.md` (via `render_invite_markdown`) et `OpenCodeRunner.run_summary(..., invite_path)` n'ajoute la clause d'instruction que si le fichier existe. Le brief est **indicatif** (cf. prompt v2.7 §4bis) : le comptage acoustique reste prioritaire, aucune correspondance 1:1 n'est forcée. Comme `## Participants probables` pré-remplit l'étape Participants, l'orthographe issue de l'invitation se propage jusqu'aux préfixes locuteurs du SRT après validation humaine.

**`CohereTranscriber.transcribe()` accepte deux formes d'entrée :**
- `transcribe(audio_path=Path(...))` — charge l'audio depuis le disque (usage standard)
- `transcribe(audio_path=None, audio_array=np.ndarray, sample_rate=16000)` — audio déjà en mémoire (chunking par tours, évite les I/O)

### Workflow (9 étapes affichées)
Le wizard guide l'utilisateur de l'upload au package ZIP. Chaque étape correspond à un `JobState`. Les transitions passent obligatoirement par `workflow/transitions.py`. Voir `docs/DATA_MODEL.md` pour le détail des états.

**Contrat d'état pendant le résumé :** `run_summary()` reste en `SUMMARY_RUNNING` du début jusqu'à `SUMMARY_DONE`. Sa sous-phase de diarisation appelle `run_speaker_detection(..., update_state=False)` : elle **ne doit pas** publier `SPEAKER_DETECTION_RUNNING`/`DONE` (états « en avant » du wizard, classés étape Participants), sinon `compute_statuses()` marquerait `summary=DONE` et le template afficherait un cadre « Contexte » vide avant que `meeting_context.json` ne soit écrit. La diarisation y est best-effort (un échec n'écrase pas l'état, le résumé poursuit). Les états `SPEAKER_DETECTION_*` ne sont publiés que par la détection manuelle (`POST /api/jobs/<id>/speakers/detect`), après `SUMMARY_DONE`.

**VRAM insuffisante = attente, jamais FAILED :** une indisponibilité VRAM est transitoire. Les phases GPU (`run_transcription`, `run_diarization`, `run_speaker_detection`, `_run_quick_transcription`) renvoient un signal `{"vram_wait": True, "required_mb", "phase"}` au lieu d'appeler `update_state(FAILED)` ; seul le `except Exception` générique reste un échec réel. **File principale** : `PipelineService` propage `vram_wait`, `JobExecutorService._run_process` re-queue (`requeue_later`) + `mark_execution_waiting_vram` (statut d'exécution non terminal) — pas d'e-mail d'échec propriétaire ; le scheduler reprend dès admission VRAM possible. **STT distant** : `_run_quick_transcription` saute la réservation VRAM locale quand `summary_stt` est une phase distante (`_phase_runs_remotely`) — pas de réservation fantôme ni d'attente VRAM à tort sur un tier sans GPU (aligné sur `_reserve_gpu_phase`). **Résumé synchrone** : `api_summary` restaure l'état pré-résumé et **enfile une reprise serveur** (mode de file `summary`, profil VRAM `summary_stt`) que le scheduler reprend même sans page ouverte ; `_run_process` exécute alors `run_summary` (qui pose `SUMMARY_DONE`/`FAILED`) sans marquer `COMPLETED` ni notifier le propriétaire. Le wizard poll `GET /status` et recharge à `summary_done` (`api_summary` refuse une relance synchrone tant qu'une entrée `summary` est active → pas de double-run). **Frontal `role=web` (split, sans GPU)** : **aucune** étape GPU synchrone ne s'exécute sur le frontal — `api_summary` ET `api_speakers_detect` (détection pyannote) **enfilent** sur le worker GPU (modes `summary`/`speakers`, `JobExecutorService.STEP_MODES`) ; décision sur le **rôle**, pas le matériel. L'admin est alerté **une seule fois** par épisode (`alert_admin_vram_wait` : e-mail + log + bandeau `JobStore.count_waiting_vram`) ; anti-spam via le drapeau persistant `extra_data.vram_alert_sent`, réarmé seulement aux transitions terminales. TranscrIA **ne tue jamais** un process GPU **tiers** par défaut. En revanche, si un STT/diarisation manque de VRAM et que **notre propre** LLM d'arbitrage **inactive** la détient, on l'**arrête** proprement (verrou LLM libre — jamais une LLM en service ; relancée à la phase de correction) via le helper partagé `transcria/gpu/vram_reclaim.stop_idle_arbitrage_llm`, à **deux niveaux** : en cours de phase (`WorkflowRunner._reclaim_vram_from_idle_arbitrage_llm`, sur `GPUSessionError`) **et à l'admission du scheduler** (`QueueScheduler._resources_available`, avant dispatch — sinon un job en file reste `waiting` indéfiniment derrière notre LLM chaude). Le déclencheur du reclaim est toujours une phase **non-LLM**. **VRAM de la LLM = besoin MULTI-GPU (audit 11/06/2026)** : la LLM s'étale sur les cartes de son script (`gpu.llm_gpu_indices`, défaut tous les GPU ; `gpu.llm_vram_mb` = empreinte TOTALE — à recalibrer à chaque changement de modèle). Réservation par `GPUAllocator.try_reserve_llm` (une part = total ÷ nb cartes par GPU, tout-ou-rien, libérée d'un bloc) — JAMAIS `try_reserve` mono-GPU pour la LLM (60 Go ne tiennent dans aucune carte : c'était insatisfaisable par construction → deadlock vram_wait à toute relance). À l'admission, le drapeau stocké `llm_shared` est HÉRITÉ (il était inconditionnellement vrai) : `QueueScheduler._llm_admissible` interroge la **vérité vivante** (LLM en marche → partagée ; éteinte → `can_host_llm` requis) et `_local_required_mb` exclut toujours `llm_arbitration` du max mono-GPU. Politique **`gpu.preemption`** (réglable dans `/admin/config` → « Ressources GPU ») : `own-only` (défaut) = catégorie 1 seulement ; `aggressive` = préempte aussi les serveurs d'inférence tiers (`kill_patterns`, non trackés) **dans la fenêtre calendaire `force_gpu`** uniquement. Voir `docs/SERVICE_RESSOURCES_GPU.md` §7.2-bis.

**Pipeline reprenable (checkpoint/resume) :** `PipelineService._run_pipeline_steps` **saute les phases déjà faites** et reprend à la première incomplète, au lieu de tout refaire au re-dispatch. Source de vérité **v2 (provenance)** : une phase n'est sautée que si marqueur `extra_data.pipeline.completed_phases` **ET** artefact déclaré présent **ET** empreintes sha256 de ses **entrées** inchangées depuis son checkpoint (`extra_data.pipeline.phase_inputs`, `transcria/workflow/resume.py` : `_PHASE_INPUTS`, `phase_state_valid`). Quand une phase amont se rejoue, les empreintes des phases aval ne correspondent plus → **ré-exécution automatique** (jamais de rapport qualité/export calculé sur du périmé — régression réelle du job 4bda98cb) ; au mismatch, le marqueur est **retiré en base** (`unmark_phase`) avant d'exécuter, donc l'admission et l'UI restent vraies même si un `vram_wait` coupe la chaîne. **Règles** : toute **nouvelle phase déclare ses entrées dans `_PHASE_INPUTS`** ; fraîcheur par **contenu (sha256), jamais par mtime** (le pull `pg` ne préserve pas les mtimes) ; **doute → re-run** (marqueur sans empreintes, artefact manquant ⇒ rejouer) ; rétro-remplissage « artefact ⇒ fait » restreint à `transcription` ; l'**audio est exclu des empreintes** (délibéré, cf. doc). Le préprocess (transforms audio) est un checkpoint unique qui persiste `audio_path`. **Conséquences** : un re-queue (`vram_wait`/`deferred`) **ne refait plus** le STT ; l'admission n'exige que la VRAM des **phases restantes** (`QueueScheduler._done_profile_phases` → `_local_required_mb`) ; `run_correction` revient au contrat `vram_wait` (le re-queue reprend à la correction, sans boucle ni worker figé). L'état est **réinitialisé** à une re-soumission utilisateur (`api_process` → `reset_resume_state`) et **préservé** sur les re-queues automatiques. Depuis le chantier stockage partagé : le **checkpoint pousse les artefacts en base AVANT le marqueur** (`_checkpoint` → `artifact_store.push_job_files` puis `mark_phase_done`), et un `audio_path` mémorisé **absent du disque local** fait rejouer le préprocess (reprise portable entre workers). Voir `docs/PIPELINE_REPRISE.md` (§10 pour la v2).

**Isolation des agents LLM (AgentWorkspace) :** **aucun agent opencode ne tourne dans un répertoire canonique du job NI dans l'arbre du dépôt** — deux incidents réels. (1) job 4bda98cb : l'agent de correction (cwd=`metadata/`, Edit actif) a réécrit `transcription.srt`, l'artefact SOURCE, sapant « l'artefact fait foi ». (2) job 6f4f4cad : le scratch sous `jobs/<id>/work/` étant dans l'arbre git, opencode (qui détermine sa racine de projet en **remontant** depuis le cwd) chargeait le `AGENTS.md` (~95 Ko) dans le contexte de chaque agent étroit ET ancrait `bash`/`read`/`write` sur la racine git → l'agent de relecture déraillait (chemins relatifs cassés → `FileNotFoundError`, puis évasion `/tmp` rejetée en headless → run avorté, 2/4 fichiers en silence). Toute phase agent (correction, relecture finale, résumé) passe par `transcria/workflow/agent_workspace.py` : scratch **`<storage.agent_work_dir>/<job_id>/<phase>/` HORS de l'arbre du dépôt** (défaut : `<tempdir>/transcria-agent-work/`, via `resolve_agent_work_root(config)`) → opencode prend le scratch comme racine de projet (contexte propre sans `AGENTS.md`, chemins relatifs fiables) ; `TMPDIR` est pointé sur le scratch (temporaires réflexes in-project). Le scratch contient des **copies** des entrées (`stage`) ou du matériel de prompt transitoire (`write_input`) ; il n'est ni sous `job_dir` ni dans `SYNCED_PREFIXES` (jamais en base, jamais re-matérialisé au pull). Le runner **collecte** les sorties du scratch et écrit lui-même le canonique (atomique) ; `verify_and_restore_sources()` post-run **restaure** tout fichier stagé muté et **signale** toute altération d'un canonique surveillé (`metadata/`, `context/`, `summary/`). Scratch supprimé après succès, conservé pour diagnostic après échec ; `AgentWorkspace.purge_job()` le nettoie à la suppression du job (hors `job_dir`, donc non couvert par `rmtree(job_dir)`). **`AGENTS.md` reste à la racine du dépôt** (pour les agents de codage) : c'est le lieu d'EXÉCUTION des phases pipeline qui change, pas l'emplacement d'`AGENTS.md`. Toute nouvelle phase agent suit ce motif ET passe `work_root=resolve_agent_work_root(config)`. (3) job dbcd2bc7 (split) : même classe de panne par un autre chemin — l'agent de correction a utilisé l'outil `glob` (`**/*lexicon*`…), qui **remonte au dossier parent du scratch**, qu'opencode classe `external_directory` (permission au défaut `ask`). En headless (`opencode run`), un `ask` sans répondeur **suspend** le run → sortie jamais écrite, échec « sans production » (intermittent : ne se déclenche que si le modèle choisit de globber). Le `--dir` scratch couvre les LECTURES directes, **pas** les outils de recherche. Parade : `opencode_setup.ensure_agent_permissions(config_path, resolve_agent_work_root(config))` pose dans `opencode.json` une politique `external_directory` **déterministe** (`{"<work_root>/**": "allow", "*": "deny"}`, jamais `ask`) ; appelée aux DEUX sites de provisioning (`scripts/setup_opencode.py` hôte, `deploy/entrypoint.provision_opencode` Docker). **Règle headless : aucune permission opencode ne doit se résoudre en `ask`** (personne pour répondre) — toute permission requise par un agent se pré-accorde (`allow`) ou se refuse (`deny`).

**Stockage des fichiers de jobs (split sans filesystem partagé) :** en topologie `role=web` / `role=scheduler` multi-machines, les fichiers d'un job suivent le même chemin que son état : **PostgreSQL** (`storage.shared_backend: pg`, défaut `fs` = comportement historique). Module unique : `transcria/jobs/artifact_store.py` (tables `job_files`/`job_file_chunks`, chunks 8 Mo, sha256 vérifié, manifeste local `.sync_state.json`, règle « jamais écraser un fichier local modifié non poussé »). Points d'accroche — frontale : push à l'upload (`JobService.upload`), à l'enfilage (`submit_process`, préfixes `INPUT_PREFIXES`) et **après toute écriture HTTP réussie** (`after_app_request` global : tout endpoint d'écriture futur est couvert d'office) ; pull paresseux throttlé en `before_app_request` ; package zip **reconstruit localement** à la demande (exclu de la synchro, il contient l'audio). Worker : pull au début de `_run_process`, push au checkpoint de phase et en fin d'exécution, **purge des blobs `input/`** aux états terminaux du pipeline complet (pas après une étape `summary`/`speakers`). Le nœud de ressources ne stocke jamais de fichier utilisateur. **Règle d'or : ne jamais supposer un disque commun entre tiers** — tout nouveau fichier nécessaire à un autre tier doit vivre sous un préfixe synchronisé (`SYNCED_PREFIXES`) ou en base. `transcria doctor` vérifie la cohérence rôle/backend. Voir `docs/STOCKAGE_PARTAGE_JOBS.md`.

**Échec silencieux LLM (résumé) = retry ≤ 3 puis blocage relançable :** opencode peut « réussir » (exit 0) sans rien produire (0 texte, `summary.md` non réécrit). `OpenCodeRunner.run_summary` le détecte par le **mtime de `summary.md` avant/après** le run (jamais par matching de chaîne, fragile) et expose `_summary_produced`. `_run_llm_summary` retente la **seule** phase LLM jusqu'à 3 fois (pas de re-STT). Après 3 échecs : `meeting_context` non corrompu, job **non** `SUMMARY_DONE` (drapeau `extra_data.summary_llm_failed`), wizard affiche un bandeau, résumé **relançable** — la relance réutilise le transcript en cache (`_load_cached_quick_summary`, pas de STT GPU). Le placeholder de `SummaryGenerator` n'est **jamais** stocké comme résumé. Diagnostic a priori : `transcria doctor --llm-smoke`.

**Garde anti-concurrence des phases synchrones :** `api_summary` et `api_speakers_detect` exécutent leur pipeline dans le thread HTTP et publient un état `RUNNING` pour toute leur durée. Ils renvoient `409` si le job est déjà `SUMMARY_RUNNING` / `SPEAKER_DETECTION_RUNNING` (anti double-lancement GPU et course sur `meeting_context.json`). `api_process` garde déjà la file via `can_start_processing` + `is_execution_active`.

### Chat d'affinage des livrables (mode de file `refine`)
Post-workflow, sur la page **`/jobs/<id>/result`** d'un job TERMINÉ (tous profils) — atteignable depuis l'étape Export du wizard et les cartes de l'accueil. L'utilisateur discute des livrables avec la LLM locale puis applique des modifications. Deux modes, contrat asymétrique :
- **`discuss` = appel LLM DIRECT, lecture seule** (`workflow/refine_llm.py`) : un seul `/v1/chat/completions` (~1,6 s mesuré vs 45-55 s en boucle agentique opencode). Le system message inline les livrables (synthèse, SRT tronqué à `max_transcript_chars`, données structurées, options de rendu, points qualité `quality/review_points.json` dont variantes lexique) ; l'historique (`context_turns` derniers tours) est rejoué en VRAIS tours user/assistant. **Piège modèles thinking (Qwen3.x)** : tout le budget `max_tokens` part dans `reasoning_content`, `content` vide → payload `chat_template_kwargs: {enable_thinking: false}` (retry sans le champ si le backend le rejette) + filtre `<think>`. Chaque réponse se termine par le label CONTRACTUEL « Proposition d'application : … » (ou « aucune »), extrait côté serveur (`refine_store.extract_proposal`, tolérant label-sans-`---`) → carte + bouton « Appliquer cette proposition ».
- **`apply` = opencode dans un `AgentWorkspace`** (édition de fichiers sous garde-fous) : `_corrected_srt_integrity_error` réutilisé, JSON normalisé, `_sanitize_render_options`, **snapshot de version AVANT write-back** (`refine/versions/v<N>/` + manifeste mémorisant aussi les fichiers ABSENTS — le revert supprime les créés), restauration via API. **Périmètre par défaut = TOUS les livrables** : les prompts cadrent « la demande définit le CHANGEMENT, pas un fichier » — un terme corrigé l'est dans synthèse + SRT + structured de façon cohérente sauf restriction explicite. Best-effort intégral : tout échec = tour assistant explicatif, livrables intacts.

Règles/pièges spécifiques : (1) chaque tour transite par la **file** (mode `refine` de `STEP_MODES`) — le job étant terminé, ses blobs `input/` sont purgés : le scheduler **dispatche `refine` SANS audio** (`_dispatch_iteration`, `audio_arg=""`) alors que tous les autres modes l'exigent (warning explicite sinon) ; (2) toute phase LLM réserve via **`try_reserve_llm`** (réparti multi-GPU), jamais `try_reserve` mono-GPU ; (3) le DOCX est régénéré à CHAQUE téléchargement et le ZIP rebuilt → le write-back suffit, mais l'UI doit le dire (note `#refine-fresh-note`, message de fin d'apply) sinon l'utilisateur croit ses fichiers périmés ; (4) les aperçus SRT à l'écran passent par `_effective_srt(fs)` (corrigé sinon brut — même préférence que `/download/srt`) ; (5) les **options de rendu** (`context/render_options.json` : thème, sections) ont une route directe SANS LLM (`POST /refine/render-options`, instantané). Réf. : `docs/TECHNICAL.md` (run_refine), `docs/CONFIG_REFERENCE.md` (`workflow.refine_chat`).

### Éditeur de transcription intégré (atelier /jobs/<id>/editor)
Cf. `docs/EDITEUR_SRT_INTEGRE.md` (cadrage + lots + retours utilisateur tracés). Règles :
1. **Le SRT effectif se lit corrigé-sinon-brut et s'écrit TOUJOURS en corrigé** —
   `workflow/srt_editor.py` est le SEUL parseur/sérialiseur (round-trip à l'octet,
   préfixe locuteur textuel `SPEAKER_XX(Nom):` tolérant). Jamais de garde de volume
   sur une édition HUMAINE (contrairement à la correction LLM) — garde de FORME seule.
2. **À la sauvegarde, stats et mapping locuteurs se recalculent et se versionnent AVEC
   le SRT** (`compute_speaker_stats`, snapshot RefineStore = pool COMMUN avec le chat
   d'affinage) — sinon le tableau des participants du DOCX ment (A2).
3. **Brouillon serveur** `metadata/srt_editor_draft.json` : verrou optimiste par
   `revision` (409), conflit détecté par `base_srt_sha256` si un affinage/une
   correction est passé — jamais de fusion silencieuse.
4. **Pics de waveform côté serveur** (`waveform_peaks.py`, ffmpeg+numpy) — ne JAMAIS
   décoder l'audio dans le navigateur (leçon du fork, mort à 3 h 30).
5. Chevauchements de timestamps AUTORISÉS à l'édition (signalés, jamais bloquants) ;
   lecture seule pendant un traitement/tour d'affinage ; sans audio = mode dégradé
   complet (tout reste éditable).
6. Le fork « SRT Editor EASY » est RETIRÉ (clés de config ignorées avec warning) —
   ne pas réintroduire de lien externe.
7. Statiques applicatifs : TOUJOURS via `asset_url()` (cache-busting mtime) — un
   `src="/static/…"` nu ressert les vieux fichiers aux navigateurs après mise à jour.

### Types de réunion personnalisés (catalogue en données)
Cf. `docs/TYPES_REUNION_PERSONNALISES.md` (cadrage + suivi des lots). Les 18 types du
rapport Word sont de la **DONNÉE** (`transcria/data/meeting_types.yaml` — noms, champs,
thèmes, drapeaux quorum/confidentiel, indices de détection, libellés courts DOCX), plus
des types **personnalisés** en base (`meeting_type_templates`, modules
`context/meeting_type_{catalog,models,store,routes,prompts}.py`) : tout utilisateur crée
(privé), les **admins partagent** (groupe/global — décalque RBAC des lexiques
centralisés). Règles pour tout agent de codage :
1. **Aucun nom/thème/champ de type en dur** dans `meeting_context.py`/`docx_report.py`
   (garde de test sur le source) — passer par `meeting_type_catalog` ; le validateur
   `validate_type_definition` est le contrat d'entrée UNIQUE (création, import).
2. **La fiche du type choisi est MATÉRIALISÉE dans le job** à l'étape 4
   (`meeting_context["custom_type"]` + `context/type_logo.png`) : rendu/worker ne
   résolvent JAMAIS un template en base (deux privés homonymes ne sont pas ambigus,
   split-safe, suppression sans casse). Type inconnu au POST contexte → 400.
3. **Rendu DOCX = registre de sections ordonnées** : `render_options.order` (job) >
   `sections.order` (fiche) > ordre historique ; `contexte`/`pv` déplaçables, jamais
   supprimables (« une donnée extraite n'est jamais cachée ») ; défaut = rendu
   historique au pixel (fixtures de non-régression dans `test_docx_section_registry.py`).
4. **Prompt de résumé** : la liste des types et les indices ne sont PLUS en dur — trois
   placeholders substitués par `OpenCodeRunner._materialize_prompt` (copie résolue dans
   le scratch ; prompt admin sans placeholder = no-op). Les `extract_fields` d'un type
   (bornés, anti-injection) s'injectent aux RELANCES + relecture finale et traversent
   le parseur via `extra_keys` (niveaux 1 ET 2) — la normalisation par liste blanche
   les filtrerait sinon.
5. **Échange** : export sans branding (logo/pied de page = local) ; import → privé
   INACTIF « à relire » (activé par édition-enregistrement) ; `community/meeting-types/`
   = contributions par PR, chaque fichier validé par un test CI.

### Modèle service/worker
`/api/jobs/<id>/process` planifie le traitement ; `JobExecutorService` l'exécute en arrière-plan. Par défaut, `workflow.queue.enabled=true` crée une entrée `job_queue` persistante et `QueueScheduler` dispatch les jobs selon priorité, calendrier et capacité (`workflow.execution.max_concurrent_jobs`, défaut 1). Supervision : `/health`, `/ready`, `/metrics`, `/api/queue/status`.

**Montée en charge (Phase B, PostgreSQL requis)** : un **rôle** sépare le tier HTTP de l'orchestrateur — `runtime.role`/`TRANSCRIA_ROLE` ∈ `all` (défaut, tout-en-un) | `web` (gunicorn -w N, n'exécute pas la file) | `scheduler` (process unique qui draine la file). Garde-fous : claim de job atomique (`QueueStore.claim`, `FOR UPDATE SKIP LOCKED`), **ordonnanceur unique** par verrou consultatif PG (`scheduler_lock.py`), réveil optionnel `LISTEN/NOTIFY` (`workflow.queue.use_listen_notify`, sinon polling), **failover actif/passif** des nœuds de ressources (`inference.nodes`). Détail : `docs/CONCURRENCE_ET_CHARGE_PHASE_B.md`. Ne jamais lancer le tier `web` avec un contexte GPU : le GPU reste dans l'orchestrateur/le nœud.

**Notifications email** : `JobExecutorService._run_process()` appelle `_notify(config, job, event, error)` juste après chaque `JobStore.update_state(COMPLETED)` ou `JobStore.update_state(FAILED)`. `_notify` délègue à `send_job_notification_async()` (module `transcria/notifications/mailer.py`) qui envoie l'email en daemon thread — jamais bloquant, absorbe toute exception. La configuration SMTP est dans `notifications.email` (`enabled`, `smtp_host`, `smtp_port`, `use_starttls`, `use_ssl`, `from_address`, `base_url`). Si `enabled=false` ou si l'adresse email de l'utilisateur est vide, aucune notification n'est envoyée. **Alerte admin VRAM** : à part du flux propriétaire ci-dessus, `notifications/admin_alerts.alert_admin_vram_wait()` prévient les comptes **ADMIN** actifs (`send_admin_vram_alert_async`) quand un job entre en attente de VRAM — une seule fois par épisode (cf. contrat d'état VRAM ci-dessus).

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
`install.sh` ne porte plus la logique métier : il fait le **bootstrap** (prérequis, choix de l'interpréteur, activation du venv) puis **délègue** chaque phase à `python -m transcria.installer.cli <phase>` (8 phases testées, à runner sous-processus injectable). Toute évolution de la logique d'installation va dans `transcria/installer/`, pas dans le shell. `scripts/bootstrap_config.py` génère `config.yaml` en fusionnant `config.example.yaml` avec les valeurs auto-détectées (`SystemDetector` : GPUs, binaires, chemins). Le fichier `.env` porte les secrets (`TRANSCRIA_SECRET`, `HF_TOKEN`).

### Déploiement conteneurisé (Docker, P5)
Un conteneur ne lance **jamais** `install.sh` : l'entrypoint applicatif est `python -m transcria.deploy.entrypoint <role>` (web|scheduler|resource-node|migrate|all) — il valide les invariants (config présente, PostgreSQL obligatoire, SQLite refusé), attend la base, puis `exec` le serveur du rôle. `migrate` est un job one-shot ; `all` = tout-en-un de test. L'accès GPU passe par **CDI** (`--device nvidia.com/gpu=…`, pas `--gpus all`) ; activation hôte via `scripts/setup_docker_gpu.sh`. Détails : `docs/DOCKER.md`.

Deux invariants d'image à respecter (vérifiés par les 3 modes en E2E, 2026-06-23) :
- **opencode dans TOUTE image dont un rôle exécute des phases LLM** (`scheduler`, `all`). Les images `install.sh` (`Dockerfile.worker`/`Dockerfile.resource-node`) l'installent via la SECTION 9 d'install.sh ; l'image de base (`Dockerfile`, construite par `pip install`) l'installe **explicitement** via l'installateur officiel (sinon les phases LLM échouent — bug corrigé). Ajouter un rôle LLM ⇒ s'assurer qu'opencode est dans son image.
- **La LLM d'arbitrage n'est JAMAIS embarquée dans l'image applicative** (ni base, ni all-in-one) : c'est toujours un endpoint OpenAI externe. En all-in-one, pour une LLM sur l'hôte → `TRANSCRIA_ARBITRAGE_LLM_HOST=host.docker.internal` + `extra_hosts: ["host.docker.internal:host-gateway"]` (cf. source unique de résolution, section opencode).

## Pièges connus

### Sentinelle `_apply_llm_suggestions` — comparaison exacte uniquement
`WorkflowRunner._apply_llm_suggestions()` (runner.py) garde un test d'early return pour détecter un résumé indisponible. Ce test est intentionnellement une **comparaison exacte** :
```python
if not summary_text or summary_text.strip() == "Résumé indisponible.":
```
Ne jamais le remplacer par `"indisponible" in summary_text.lower()` : un résumé valide peut contenir ce mot dans son corps (ex : "fallback quand X est indisponible"), ce qui causerait un faux positif silencieux — `meeting_context.json` resterait non mis à jour sans aucun log d'erreur. La sentinelle `"Résumé indisponible."` est la seule valeur retournée par `run_summary()` quand opencode ne produit rien.

### opencode — provider `local` requis dans `~/.config/opencode/opencode.json`
`OpenCodeRunner` invoque opencode avec `--model <provider>/<model>` depuis `workflow.summary_llm.model_id` ou `workflow.arbitration_llm.model_id` (exemple : `local/arbitrage`). Dans opencode, le préfixe `local/` désigne un provider nommé `local`. Ce provider **doit** être déclaré dans `~/.config/opencode/opencode.json` pointant sur le serveur llama.cpp (port 8080 par défaut, ou `NODE_IP:8080` en topologie distribuée). Sans cette entrée, opencode ne sait pas résoudre `local/` → les appels LLM échouent silencieusement et `summary.md` conserve le placeholder.

**Hôte/port de l'arbitrage = SOURCE UNIQUE.** L'URL du provider opencode (écrite au boot conteneur par `provision_opencode` → `opencode_setup.default_base_url`) **et** la sonde/cycle de vie (`VRAMManager`) résolvent l'endpoint via la **même** fonction `opencode_setup.resolve_arbitrage_endpoint` : hôte = env `TRANSCRIA_ARBITRAGE_LLM_HOST` > `services.arbitrage_llm_host` > `127.0.0.1` ; port = `services.arbitrage_llm_port` (compat `qwen_port`) défaut 8080. **Ne JAMAIS relire `services.arbitrage_llm_host` à la main ailleurs** — sinon opencode et la sonde divergent (bug réel : opencode pointait `127.0.0.1` quand l'hôte n'était fixé que par l'env).

**Ne pas écrire ce fichier à la main** : utiliser `venv/bin/python scripts/setup_opencode.py` (idempotent, format correct, ne casse pas une config existante). Helper sous-jacent : `transcria/gpu/opencode_setup.py` (`find_opencode_binary`, `ensure_local_provider`, `resolve_arbitrage_endpoint`). Format **courant** produit (≠ ancien `providers`/`type:openai`/`validate`) :
```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "local": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "LLM d'arbitrage (local)",
      "options": { "baseURL": "http://127.0.0.1:8080/v1", "apiKey": "dummy-key", "timeout": 9999999 },
      "models": { "arbitrage": { "name": "LLM d'arbitrage (local)" } }
    }
  }
}
```
**Convention : alias générique `arbitrage`.** On utilise délibérément le **même nom stable** partout — clé `models` d'opencode (`arbitrage`), `model_id` de config (`local/arbitrage`), `--alias` de llama-server (`arbitrage`) et `services.arbitrage_api_model_id` (`arbitrage`). But : pouvoir **changer le modèle réellement servi** (palier VRAM : LFM2.5-8B, Qwen3.5-9B, Gemma 4, Qwen3.6-35B…) en ne touchant **qu'au script de lancement**, sans jamais modifier `config.yaml` ni `opencode.json`. L'alias ne décrit donc PAS le modèle servi — c'est voulu. llama.cpp ignore de toute façon le champ `model` de la requête et sert le modèle chargé ; `services.arbitrage_api_model_id` permet au doctor de vérifier que l'alias attendu (`arbitrage`) est bien celui rapporté par le serveur.

**Profils par palier VRAM (`scripts/arbitrage_profiles/<palier>_<modèle>.sh`).** Un profil par palier (12/16/24/32/48/64 Go), chacun avec les **params d'échantillonnage OFFICIELS** du modèle (source HF en en-tête — Qwen ≈ temp 0.6, Gemma ≈ temp 1.0 ; ne JAMAIS recopier les params d'un autre modèle), KV cache Q8, et un contexte calé sur la VRAM du palier (192K pour 12/32 Go → garde ≥1 Go libre ; 256K ailleurs). Les chemins sont **portables** via `${MODELS_DIR:-…}` et `${LLAMA_SERVER:-…}` : `install.sh` écrit les bons défauts pour la machine (répertoire modèles choisi, `llama-server` **détecté ET qualifié** — module PUR `transcria/gpu/llama_runtime.py` + CLI `scripts/detect_llama_server.py` : recherche élargie, version réelle ≥ b9630 résolue via l'**arbre git** car le numéro de `--version` est **non fiable** (un vrai b9632 affiche `579`), résolution des `.so` par `ldd` (RPATH/conda), build CUDA — un binaire trouvé mais qui *ne chargera pas* est signalé à l'install, pas au 1ᵉʳ run ; **`critical` = SEULES les libs manquantes** (= ne démarre pas, indépendant du modèle), **toute version trop basse ⇒ avertissement** (besoin ≥ b9630 relatif au modèle gated-delta/gemma4 ; rien n'est de toute façon bloquant — l'install propose quand même le binaire)) au moment de la sélection, puis `scripts/switch_arbitrage_llm.sh <palier>` recopie le profil choisi sur `launch_arbitrage.sh`. Les profils honorent un `LLAMA_LD_LIBRARY_PATH` fourni par l'environnement (escape-hatch pour un env de libs non standard). La **sélection du palier** se fait par **placement réel** (module PUR `transcria/gpu/llm_placement.py` : empreintes mesurées + faisabilité mono/split carte par carte — jamais le choix « à la somme » qui OOMait sur 2×8 Go, cartes hétérogènes, ou 2 cartes pour un profil 3-cartes), et la **calibration VRAM par carte** (`gpu.llm_vram_mb`/`llm_gpu_indices`/`llm_vram_mb_per_gpu`) est écrite par le planner (`scripts/plan_llm_placement.py … --apply` via `transcria/config/gpu_calibration.py` — round-trip ruamel non destructif, **pas** le `sed` du `switch` qui échoue en silence sur les listes YAML par blocs). `scripts/check_arbitrage_llm.sh` (mode `verify`) **mesure** ensuite la VRAM réelle par carte vs le déclaré (dérive / marge critique / débordement) **et qualifie le binaire `llama-server`** (version git / libs / CUDA, via le détecteur). **Toute nouvelle quantification/modèle de palier doit être validée par lecture** (cf. `docs/BENCH_LLM_PALIERS.md`) — pas un score auto : un Q4 peut émettre des artefacts (glyphes/JSON cassés) qu'un Q5/Q6 du même modèle n'a pas.

### `correction_prompt.txt` — version courante : v2.2

**v2.2 (2026-06) — application du lexique par la LLM + délégation @general obligatoire** : la fausse prémisse « les variantes du lexique sont déjà appliquées par le système » est supprimée (aucune étape système ne les applique ; seuls les **locuteurs** le sont). La LLM est désormais responsable d'**appliquer le lexique validé en contexte** (section 2bis) : pour chaque entrée non `_preservation_only`, remplacer une variante par la forme cible (`replace_by` sinon `term`) **uniquement** quand le contexte confirme le terme, jamais un homographe légitime — pas de substitution aveugle par script. Le rapport documente substitutions appliquées et variantes non remplacées. La délégation à des subagents **@general est obligatoire** (plages disjointes traitées **séquentiellement** pour éviter une course à l'écriture sur `transcription_corrigee.srt`).

**summary_prompt.txt v2.6 (2026-06) — délégation @general obligatoire + synthèse de qualité** : la délégation @general devient obligatoire (plages disjointes ; l'agent principal est le **seul** à écrire `summary.md` — pas de course à l'écriture — et fusionne/dédoublonne les constats des subagents, §5.3). La Synthèse n'est plus plafonnée à « 8 à 15 lignes » : elle est **proportionnée à la durée** (≈ 1 paragraphe par point d'ODJ/thème) et de haute qualité, avec un garde-fou de fidélité (jamais inventer décision, chiffre, nom ou échéance).

**summary_prompt.txt v2.7 (2026-06) — brief d'invitation + animateur = fil de structure** : nouvelle entrée n°4 (`meeting_invite.md`) et **section 4bis**. Le brief est **indicatif, jamais autoritaire** : le nombre de voix détectées prime (invités ≠ présents), aucune correspondance 1:1 n'est forcée, et il sert seulement à l'orthographe des noms (`## Participants probables`), aux rôles annoncés et à la structure de l'ordre du jour. L'**animateur** (déduit du brief/transcription, car le résumé tourne avant la validation des participants) est traité comme **fil de structure** — agenda, transitions, points mis en décision — mais **sans crédit factuel supplémentaire** : chaque contenu est attribué à qui le dit réellement et les faits de chaque locuteur pèsent à égalité (pas de biais vers le facilitateur). Aucune section de sortie n'est modifiée : le parser `_parse_structured_summary` reste compatible.

**summary_prompt.txt v2.8 (2026-06) — genre hors nom/rôle** : §4 interdit d'écrire le genre (Masculin/Féminin, ♂/♀) dans le nom ou le rôle d'un participant (c'est un indice acoustique interne, à champ dédié dans l'UI). Garde déterministe côté code : `OpenCodeRunner._strip_role_gender()` retire un marqueur de genre en fin de rôle lors du parsing de `## Participants probables` (ponctuation de phrase préservée, « masculin » au milieu d'une phrase non touché).

**Rendu Markdown du DOCX :** le résumé LLM est du Markdown. `docx_report.py` ne **supprime** plus les `**…**` : `_split_markdown_bold()` (fonction pure) découpe le texte en segments (contenu, gras) et `_add_markdown_runs()` les rend en runs **gras réels**. La Synthèse rend aussi les intertitres (`##` → ligne en gras détachée), les puces (`-`/`*`) et un espacement de paragraphe — ce qui corrige à la fois le gras non rendu et l'effet « tassé » (qui venait du code, pas de la LLM).

**Phase de relecture finale (`run_final_review`, A+C+D+G) :** la synthèse et les données structurées sont produites à l'étape résumé, **avant** la correction et la validation humaine ; elles gardent donc des graphies ASR (`AKRO`, `Jean Dupon`, `Marie`) que la correction du SRT ne touche jamais. Une **phase pipeline dédiée**, insérée **après `correction` et avant `quality`** (`pipeline_service._define_pipeline_steps`), réutilise la LLM d'arbitrage déjà chargée pour, en **une seule session opencode** (prompt dédié `final_review_prompt.txt`, **pas** le gros prompt de correction) :
- **A** — harmoniser la synthèse (`summary_llm`) sur le **glossaire validé** (`build_harmonization_glossary(participants, lexicon)` : noms validés + formes canoniques ← variantes) ;
- **C** — rendre les noms/termes du glossaire **cohérents dans tout le SRT corrigé** (mêmes formes d'une plage à l'autre — un travail que la correction, découpée par plages en @general, ne peut pas faire) ;
- **D** — résoudre les **variantes de lexique** encore présentes dans le SRT ;
- **G** — **auditer les données structurées** (décisions/actions/chiffres/dates) contre le SRT : corrige nom/chiffre/date divergents, **marque `[À VÉRIFIER]`** (jamais supprime) les éléments non étayés, strictesse graduée selon le type de réunion (🔴 CSE/client/négociation/crise/entretien/médical, 🟠 décisionnels, 🟢 formation/podcast).

Délégation **@general obligatoire** pour le SRT (C+D, plages disjointes) ; A et G traités par l'agent principal (textes courts). Sorties appliquées par `_apply_final_review()` avec garde-fous : SRT relu accepté **seulement** si ratio de taille 0.9–1.1 (anti-troncature), synthèse → `meeting_context["summary_harmonized"]`, données structurées relues → `meeting_context["structured_data"]` si JSON valide, rapport → `metadata/final_review_report.md`. Le DOCX choisit la synthèse dans l'ordre **`summary` (édition manuelle étape 4) > `summary_harmonized` > `summary_llm`** (l'édition humaine n'est jamais écrasée). **Best-effort** : la phase renvoie toujours `success=True` — un échec n'interrompt jamais le pipeline (correction et résumé restent valables). Progression : 83 % → 89 % (qualité décalée à 90–92 %). (Note : ne réécrit pas un nom de locuteur validé dans les préfixes SRT si l'humain a saisi une coquille — cf. garde déterministe différée [[speaker_name_srt_guard]].)

**summary_prompt.txt v2.0 (2026-05-19)** : restructuration complète. Points critiques pour la compatibilité parser : section `## Participants probables` (match exact), section `## Termes douteux à valider` (match `## Termes (?:suspects|douteux).*?`), format terme `**TERME** [cat] (prio) | variantes_suspectes: ... | commentaire: ... | contextes: ...`, `(aucune)` filtré par `empty_markers`, séparateur `||` pour contextes multiples (`_parse_summary_contexts`), `(non identifiable)` pour participants absents. Le parser ignore uniquement les vrais placeholders `non identifiable` ; il conserve une ligne `SPEAKER_XX [label]` si ces mots apparaissent dans le texte du rôle.

**v1.7 (2026-05-18) — vérification par sous-agent** : section 15 ajoutée. Après écriture des fichiers, un sous-agent relit le SRT corrigé et le lexique depuis le disque à froid, croise avec les corrections déclarées dans le rapport pour détecter les hallucinations (corrections déclarées mais non appliquées), corrige les variantes restantes, et documente le résultat dans `## Vérification sous-agent`. L'indépendance du sous-agent (lecture des fichiers réels, pas de mémoire de travail partagée) est le point clé.

**v1.6 (2026-05-18) — anti-split SRT** : la LLM peut, sur de longues transcriptions, écrire la première moitié du SRT corrigé dans `correction_report.md` et la seconde dans `transcription_corrigee.srt`. La v1.6 ferme cette ouverture via :
- Section SORTIES renforcée : `transcription_corrigee.srt` doit contenir la **totalité** des segments (1→N), `correction_report.md` est du Markdown pur (aucune ligne SRT tolérée).
- Checks 11 (complétude SRT) et 12 (séparation fichiers) ajoutés à la VÉRIFICATION FINALE.
- Instruction inline `run_correction()` mise à jour avec les mêmes contraintes.

**v1.5 (2026-05-18) — `mapped_name` immuable** : le modèle doit recopier le `mapped_name` verbatim, caractère par caractère, sans normalisation de casse, accent ou orthographe. Trois niveaux de défense : définition absolue (Section 1 LOCUTEURS), extraction préalable obligatoire de la table `speaker_id → mapped_name` avant tout segment (Étape B de la PREMIÈRE ACTION), vérification finale (check 10 de la VÉRIFICATION FINALE).
Cas concret qui a motivé cette v1.5 : `mapped_name = "marie"` → la LLM corrigeait en `"Marie DURAND"` car le prénom est prononcé dans l'audio.

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

### PostgreSQL — encodage UTF8 requis (jamais SQL_ASCII)
Une base héritée d'un cluster `initdb`-é sans locale est en `SQL_ASCII` : texte stocké **sans
validation d'encodage**, fonctions texte serveur byte-wise, et psycopg3 renvoie les colonnes
texte en `bytes` pour tout client qui ne force pas `client_encoding` (symptôme vu en local :
`StringDataRightTruncation` sur un INSERT avec un id en bytes). Défense en profondeur, à ne pas
détricoter : `install.sh` crée la base avec `ENCODING 'UTF8' TEMPLATE template0` (+ garde sur
base existante) ; `app.engine_options()` force `client_encoding=utf8` sur toutes les connexions
PostgreSQL (+ WARNING au démarrage si serveur ≠ UTF8) ; `doctor` a un check « Base de données
(encodage) » ; `tests/conftest.py` pose `PGCLIENTENCODING=UTF8` (les bases jetables pytest
héritent de `template1`). Migration d'une base existante : docs/INSTALL.md § « Encodage de la
base ». Ne jamais écrire un `CREATE DATABASE` (code, script ou doc) sans clause d'encodage.

### Réseau d'entreprise — proxy dans `.env`, modèles pré-téléchargés, jamais de download bloquant
Le service systemd n'hérite pas de l'environnement du shell : un proxy connu du seul shell rend
les téléchargements de modèles impossibles depuis le service — au pire la connexion directe est
**silencieusement absorbée** et le téléchargement **pend sans timeout** (incident SQUIM du
12/06/2026 : `urlopen` torchaudio gelé → préflight figé → job bloqué en `uploaded`). Défenses :
proxy persisté dans `.env` (lu par systemd ET python-dotenv ; `install.sh` le détecte et le
propose), check doctor « Modèles locaux (cache) » (`expected_model_assets()` pur, dérivé de la
config), timeout socket autour du chargement SQUIM (échec propre → préflight poursuivi sans
SQUIM, par design best-effort). Règle pour tout nouveau modèle : prévoir le pré-téléchargement
(docs/INSTALL.md § « Réseau d'entreprise »), l'ajouter à `expected_model_assets()`, et ne JAMAIS
introduire un téléchargement runtime sans timeout — un échec réseau doit être un échec rapide et
logué, pas un gel.

### E2E : utiliser impérativement `venv/bin/python`, pas `python`
Le Python système (3.13, `/usr/bin/python`) n'a pas accès aux packages du venv (`pyannote`, `torch`, `cohere_transcriber`). Lancer `python tests/test_e2e_workflow.py` depuis le système donne « pyannote non disponible » silencieusement. Toujours utiliser `venv/bin/python tests/test_e2e_workflow.py` ou activer le venv au préalable (`source venv/bin/activate`).

## Règles absolues

1. **Toujours** vérifier `_require_job_access(job, current_user)` dans les routes API qui modifient un job.
2. **Jamais** committer `config.yaml` (contient des chemins absolus de production) ni `.env` (secrets).
3. **Toujours** passer `config: dict` en paramètre aux fonctions du moteur, jamais `get_config()` direct (sauf dans les routes).
4. **Ne pas** modifier `JobState` ou `WORKFLOW_STEPS` sans mettre à jour `WorkflowState.compute_statuses()`.
5. **Ne pas** ajouter de nouveaux fichiers runtime dans l'arborescence job ou le stockage sensible (`voices/`) sans documenter dans `DATA_MODEL.md`. Fichiers existants à ne pas supprimer sans mise à jour de `DATA_MODEL.md` : `metadata/audio_scene.json`, `metadata/audio_quality_decision.json`, `metadata/audio_normalization.json`, `metadata/audio_scene_filter.json`, `metadata/audio_preflight.json`, `metadata/audio_denoise.json`, `metadata/stt_corpus.json`, `metadata/audio_excerpts/*.wav`, `speakers/diarization_audio.json`, `speakers/diarization_16k_mono.wav`, `speakers/diarization_checkpoint.json`, `speakers/speaker_embeddings.json`, `speakers/voice_matches.json`.
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
| `docs/DOCKER.md` | Déploiement conteneurisé : quickstart turnkey, image, compose, GPU (CDI), variables, rollback |
| `docs/TECHNICAL.md` | Architecture détaillée, flux de données, API REST, pipeline GPU |
| `docs/DATA_MODEL.md` | Schéma de données, états, transitions, arborescence disque |
| `docs/CONFIG_REFERENCE.md` | Référence complète des paramètres config.yaml |
| `docs/VAD_OR_NOT.md` | Analyse des systèmes VAD, tests comparatifs, recommandations par type de fichier |
| `docs/PARAKEET_STT_INTEGRATION.md` | Intégration du backend Parakeet TDT 0.6B v3 (NeMo) |
| `docs/SERVICE_RESSOURCES_GPU.md` | Inférence distante v1 : topologies frontale/ressources, autonomie VRAM du STT (A/B/C), `/capabilities`, mode dégradé |
| `docs/MIGRATION_API_SERVEUR_GPU.md` | Contrat d'API du nœud de ressources distant (implémenté ; renvois §4bis depuis `inference_service/`) |

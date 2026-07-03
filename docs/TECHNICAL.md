# TranscrIA — Documentation technique

## 1. Vue d'ensemble

TranscrIA est un portail guidé de transcription de réunion destiné aux utilisateurs non techniciens (secrétaires de réunion). Il orchestre le dépôt d'un fichier audio/vidéo jusqu'à la production d'un package exploitable contenant le SRT corrigé (speakers + lexique), le contexte, les participants, le lexique, le rapport qualité, le rapport de correction, les points à vérifier, et un rapport Word professionnel (.docx) prêt à distribuer.

**Stack :** Python 3.11+ / Flask / SQLAlchemy + Alembic (PostgreSQL en prod, SQLite en dev) / Jinja2 / Cohere ASR / faster-whisper large-v3 / Granite Speech expérimental / Parakeet TDT 0.6B v3 expérimental (NeMo) / pyannote / torchaudio CTC / opencode (LLM locale d'arbitrage) / Bootstrap 5 / gunicorn (montée en charge)

**Services externes :** dashboard-llm (port 5001, monitoring GPU), SRT Editor EASY (port 7861, correction manuelle)

**Note LLM :** Qwen est le modèle d'exemple du déploiement local historique. Les noms `qwen_*` conservés dans le code/config sont des aliases de compatibilité ancienne version ; le contrat courant est une LLM d'arbitrage OpenAI-compatible configurée par `services.*` et `workflow.*.model_id`.

**Démarrage (dev) :**
```bash
cd transcria
source venv/bin/activate
python app.py
# → http://0.0.0.0:7870
# Admin: admin / admin-change-me
```

**Scripts :** `./start.sh` (log `/var/log/transcrIA.log`, PID `/run/transcrIA.pid`), `./stop.sh`, `./status.sh`

**Tests :** suite pytest — `python -m pytest tests/ -q`

**Supervision locale :**
- `GET /health` retourne un statut JSON simple du service et de la base de données
- `GET /ready` retourne l’état de préparation du worker interne
- `GET /metrics` expose des métriques Prometheus légères (`transcria_up`, `transcria_jobs_total`, `transcria_jobs_state`)

---

## 2. Architecture

```
transcria/
├── app.py                         # Point d'entrée Flask (create_app + main)
├── config.yaml / config.example.yaml
├── requirements.txt
│
├── transcria/                      # Package principal
│   ├── __init__.py
│   ├── config/                    # loader.py, config_schema.py, system_detector.py
│   ├── database.py                # Instance SQLAlchemy
│   ├── diagnostics/               # doctor.py — préflight GPU-free (config, schéma DB, LLM, opencode, nœuds, dossiers) ; `--llm-smoke` opt-in = test réel opencode→LLM→texte
│   ├── logging_setup.py           # Configuration logging (RotatingFileHandler)
│   │
│   ├── auth/                      # Authentification & rôles
│   │   ├── __init__.py
│   │   ├── models.py              # User, Group, GroupMembership, Role, GroupRole
│   │   ├── groups.py              # GroupStore (CRUD groupes, membres, droits admin groupe)
│   │   ├── store.py               # UserStore (CRUD utilisateurs, count_users, ensure_admin)
│   │   ├── permissions.py         # Permission enum, _ROLE_PERMISSIONS, @requires()
│   │   └── routes.py              # Routes /login, /logout, /admin/users, /admin/groups
│   │
│   ├── jobs/                      # Gestion des traitements
│   │   ├── __init__.py
│   │   ├── models.py              # Modèle Job, JobState (20 états), JobFile/JobFileChunk (magasin pg)
│   │   ├── store.py               # JobStore (CRUD jobs, count_jobs)
│   │   ├── filesystem.py          # JobFilesystem (I/O disque, save_json/load_json/save_text/load_text/save_upload)
│   │   └── artifact_store.py      # Magasin de fichiers partagé via PostgreSQL (split web/scheduler — push/pull/purge, sha256)
│   │
│   ├── queue/                     # File persistante, scheduler et calendrier GPU
│   │   ├── allocator.py           # GPUAllocator (réservations atomiques, verrou LLM, PID tracking)
│   │   ├── store.py               # QueueStore (priorités, pause/reprise, aging)
│   │   ├── scheduler.py           # QueueScheduler (dispatch en arrière-plan)
│   │   ├── calendar.py            # SchedulingCalendar (pause_queue, limit_concurrency, force_gpu)
│   │   └── routes.py              # Pages/admin API de file et planification
│   │
│   ├── workflow/                  # Moteur de workflow 9 étapes affichées
│   │   ├── __init__.py
│   │   ├── states.py              # WorkflowState (compute_statuses, get_next_step), StepStatus
│   │   ├── steps.py               # WORKFLOW_STEPS + helpers de navigation
│   │   ├── progress.py            # WorkflowProgressReporter (progression UI persistée)
│   │   ├── resume.py              # État de reprise pipeline (completed_phases / audio_path, is_phase_done) — cf. docs/PIPELINE_REPRISE.md
│   │   └── runner.py              # WorkflowRunner
│   │
│   ├── audio/                     # Analyse et conversion audio
│   │   ├── __init__.py
│   │   ├── analyzer.py            # AudioAnalyzer (ffprobe → JSON)
│   │   ├── converter.py           # AudioConverter (ffmpeg → WAV 16kHz mono)
│   │   ├── preflight.py           # AudioPreflightAnalyzer — pré-diagnostic acoustique (RMS, SNR, bande passante, clipping, flags)
│   │   ├── vad.py                 # SileroVAD (détection de parole via faster_whisper)
│   │   ├── vad_adaptive.py        # Adaptation VAD selon audio_quality_decision
│   │   ├── vad_hysteresis.py      # HysteresisBinarizer — post-traitement hystérésis des scores VAD
│   │   ├── scene_analyzer.py      # AudioSceneAnalyzer — subprocess isolé librosa (RMS → flatness/ZCR → pitch YIN)
│   │   ├── _scene_analysis_worker.py # Worker subprocess pur pour l'analyse de scène
│   │   ├── scene_filter.py        # AudioSceneFilterService — mise en silence optionnelle pré-STT (timeline préservée)
│   │   ├── denoise.py             # AudioDenoiseService — débruitage ffmpeg optionnel (afftdn, désactivé par défaut)
│   │   ├── normalization.py       # AudioNormalizationService — normalisation ffmpeg optionnelle (timeline préservée)
│   │   └── source_separation.py   # SourceSeparationDecider + SourceSeparationService (Demucs, optionnel)
│   │
│   ├── stt/                       # Speech-to-Text
│   │   ├── __init__.py
│   │   ├── base_transcriber.py    # BaseTranscriber (ABC)
│   │   ├── cohere_transcriber.py  # CohereTranscriber (load, transcribe, segments_to_srt, offload)
│   │   ├── whisper_transcriber.py # WhisperTranscriber (faster-whisper large-v3 qualité)
│   │   ├── granite_transcriber.py # GraniteTranscriber — IBM Granite Speech 4.1 2B expérimental
│   │   ├── anti_hallucination.py  # Réduction boucles ASR répétitives
│   │   ├── forced_alignment.py    # Alignement CTC natif torchaudio optionnel
│   │   ├── speaker_realignment.py # Réalignement locuteur/ponctuation au niveau mot
│   │   ├── reliability.py          # SegmentReliabilityScorer — scoring fiabilité post-STT (ok/suspect/degrade)
│   │   ├── transcriber_factory.py # TranscriberFactory
│   │   ├── transcription.py       # Transcriber (pyannote_turns, fallback 30s, alignement, realignment)
│   │   ├── base_diarizer.py       # BaseDiarizer (ABC) — logique partagée cache/clips/embeddings/fingerprint
│   │   ├── diarization.py         # DiarizerService(BaseDiarizer) — pyannote GPU + exclusive_turns + checkpoints
│   │   ├── sortformer_diarizer.py # SortformerDiarizer(BaseDiarizer) — NVIDIA NeMo 4spk, fallback NeMo absent
│   │   ├── diarizer_factory.py    # create_diarizer(), get_diarizer_vram_mb(), list_available_backends()
│   │   ├── speaker_detection.py   # SpeakerDetector (detect + save_mapping) — utilise diarizer_factory
│   │   └── summary.py             # SummaryGenerator (VAD Silero + quick transcript)
│   │
│   ├── context/                   # Contexte de réunion
│   │   ├── __init__.py
│   │   ├── meeting_context.py     # MeetingContextManager + MEETING_TYPES
│   │   ├── participants.py        # ParticipantsManager + default_participant
│   │   ├── invite_parser.py       # sanitize_invite/render_invite_markdown — brief d'invitation (noms via e-mails, e-mails retirés)
│   │   ├── lexicon.py             # LexiconManager + LEXICON_CATEGORIES/PRIORITIES
│   │   └── job_context_builder.py # JobContextBuilder (build → YAML + JSON)
│   │
│   ├── quality/                   # Contrôle qualité non destructif
│   │   ├── __init__.py
│   │   ├── srt_checks.py          # SRTChecker (check_segment, check_segments)
│   │   ├── lexicon_checks.py      # LexiconChecker (check → found/missing/variants_found)
│   │   ├── review_points.py       # ReviewPoints (generate → phrases utilisateur)
│   │   ├── audio_quality.py       # Diagnostic qualité audio et signal éventuel de forçage backend
│   │   └── quality_report.py      # QualityReporter (contrôles, score /100, markdown)
│   │
│   ├── exports/                   # Exports finaux (ZIP + rapport DOCX)
│   │   ├── __init__.py
│   │   ├── package_builder.py    # PackageBuilder (ZIP avec tous les fichiers + rapport DOCX)
│   │   └── docx_report.py        # DocxReport — rapport Word professionnel (python-docx)
│   │
│   ├── integrations/              # Services externes
│   │   ├── __init__.py
│   │   ├── dashboard_client.py    # DashboardClient (API REST :5001)
│   │   └── srt_editor_link.py    # SrtEditorLink (push audio/SRT + resolve_public_url)
│   │
│   ├── gpu/                       # Gestion GPU
│   │   ├── __init__.py
│   │   ├── vram_manager.py        # VRAMManager
│   │   ├── gpu_session.py         # GPUSession context manager
│   │   ├── llm_backend.py         # LLMBackend + 3 implémentations + factory
│   │   ├── opencode_runner.py     # OpenCodeRunner
│   │   ├── _port_utils.py         # is_port_open() partagé entre vram_manager et llm_backend
│   │   └── cuda_visible.py        # parse_cuda_visible_devices, to_visible_device_index, to_nvidia_smi_gpu_index
│   │
│   ├── notifications/             # Notifications applicatives
│   │   ├── __init__.py
│   │   ├── mailer.py              # EmailConfig, build_email_config(), send_job_notification_async(), send_admin_vram_alert_async(), _send_smtp()
│   │   └── admin_alerts.py        # get_admin_emails(), alert_admin_vram_wait() — alerte ADMIN « en attente de VRAM » (e-mail + log)
│   │
│   ├── services/                  # Services métier
│   │   ├── job_executor.py       # JobExecutorService (worker thread) + _notify() hook email
│   │   ├── job_service.py        # JobService
│   │   ├── pipeline_service.py   # PipelineService
│   │   └── config_service.py     # ConfigService
│   │
│   ├── voice/                     # Voix enregistrées
│   │   ├── models.py              # SQLAlchemy sujets, consentements, profils, matches, audit
│   │   ├── store.py               # Périmètre groupe, stockage, audit
│   │   ├── embedding.py           # Empreintes vocales pyannote + cosine
│   │   ├── enrollment.py          # Génération empreinte depuis audio de référence
│   │   ├── matching.py            # Matching job→voix connues
│   │   ├── consent_form.py        # PDF vierge de consentement
│   │   └── routes.py              # /admin/voices
│   │
│   └── web/                       # Interface utilisateur
│       ├── __init__.py
│       ├── routes.py              # Routes Flask (pages + API REST)
│       ├── ui_labels.py           # Libellés FR des états de job (filtres Jinja state_label/state_badge)
│       ├── prompt_files.py        # Édition web des prompts LLM (liste fermée, .bak, atomique) + scripts lecture seule
│       ├── static/css/transcria.css  # Feuille de style applicative (tokens — docs/archive/REFONTE_UI.md)
│       └── templates/             # Templates Jinja2 (Bootstrap 5)
│           ├── base.html
│           ├── login.html
│           ├── change_password.html
│           ├── index.html
│           ├── job_wizard.html
│           ├── job_result.html
│           ├── dashboard_status.html
│           ├── admin_config.html
│           ├── users.html
│           ├── user_form.html
│           ├── groups.html
│           ├── group_form.html
│           ├── voices.html
│           ├── voice_form.html
│           └── voice_detail.html
│
├── configs/                       # Prompts et lexique
│   ├── lexique_metier.txt
│   └── prompts/
│       ├── summary_prompt.txt      # Prompt résumé structuré (opencode) — v3.0 (contrat de priorités en tête, consigne subagent citable, @general obligatoire)
│       ├── correction_prompt.txt   # Prompt correction SRT (speakers + application lexique en contexte + orthographe) — v3.0 (contrat de priorités, SPEAKER_XX(nom) intouchable)
│       ├── final_review_prompt.txt  # Relecture finale A+C+D+G (synthèse/SRT/données structurées) — v3.0, après correction
│
├── tests/                         # suite pytest + E2E (2500+ tests)
│   ├── conftest.py                # Fixtures (app, client, admin/operator/viewer)
│   ├── test_audio.py              # 64 tests — Analyse de scène worker, AudioSceneAnalyzer, séparation sources, genre
│   ├── test_audit.py              # 12 tests — AuditStore, rétention par famille
│   ├── test_auth.py               # 17 tests — Rôles, modèles, permissions
│   ├── test_auth_store.py         # 14 tests — CRUD utilisateurs, groupes
│   ├── test_bench_tools.py        # 13 tests — Outils benchmark audio
│   ├── test_central_lexicon.py    # 28 tests — LexiconStore, LexiconService, routes admin
│   ├── test_config.py             # 40 tests — Chargement YAML, sauvegarde config, debug
│   ├── test_context.py            # 27 tests — Meeting, participants, lexique, builder
│   ├── test_diarization.py        # 37 tests — DiarizerService, SortformerDiarizer, BaseDiarizer, factory
│   ├── test_edge_cases.py         # 17 tests — Cas limites contexte/exports/transitions
│   ├── test_exports.py            # 3 tests — PackageBuilder
│   ├── test_gpu.py                # 72 tests — VRAMManager, CUDA_VISIBLE_DEVICES, libération VRAM ciblée, diagnostic lancement LLM
│   ├── test_gpu_allocator.py      # 7 tests — Réservations GPU, remapping CUDA visible, verrou LLM
│   ├── test_integrations.py       # 12 tests — DashboardClient, SrtEditorLink, OpenCodeRunner
│   ├── test_job_service.py        # 2 tests — JobService
│   ├── test_job_store.py          # 15 tests — JobStore CRUD, purge rétention
│   ├── test_jobs.py               # 19 tests — Job model, filesystem
│   ├── test_job_executor_vram_wait.py # 2 tests — re-queue + alerte admin sur VRAM, mode summary
│   ├── test_mailer.py             # 20 tests — EmailConfig, templates, async dispatch, modes SMTP
│   ├── test_opencode_runner.py    # 54 tests — opencode, parsing résumé, correction
│   ├── test_pipeline_service.py   # 19 tests — Analyse de scène, séparation, filtrage, normalisation, ordre pipeline
│   ├── test_quality.py            # 19 tests — SRTChecker, LexiconChecker
│   ├── test_quality_deep.py       # 37 tests — Tests approfondis qualité avec SRT réel
│   ├── test_queue_calendar.py     # 10 tests — SchedulingCalendar, règles calendrier
│   ├── test_queue_scheduler.py    # 6 tests — QueueScheduler dispatch
│   ├── test_queue_store.py        # 6 tests — QueueStore CRUD, aging
│   ├── test_stt.py                # 76 tests — STT, timestamps, alignement, speaker clips
│   ├── test_summary_generator.py  # 1 test — Résumé rapide
│   ├── test_voice.py              # 13 tests — VoiceStore, empreintes, matching
│   ├── test_voice_e2e.py          # 1 test — Flux E2E voix enregistrées
│   ├── test_vram_wait.py          # 9 tests — attente VRAM (transitions, comptage, alerte admin, route résumé, invitation)
│   ├── test_web_api.py            # 54 tests — Routes web (login, jobs, upload, admin config, lexique debug)
│   ├── test_web_edge_cases.py     # 53 tests — Erreurs API, rôles, accès jobs, pipeline
│   ├── test_web_helpers.py        # 13 tests — Helpers web (audio diagnostic, enrichissement lexique, locuteurs)
│   ├── test_workflow.py           # 30 tests — États, transitions, runner
│   └── test_workflow_runner.py    # 64 tests — Runner, correction, résumé, genre locuteur
│
├── jobs/                          # Données des traitements (runtime)
└── docs/                          # Documentation
```

---

## 3. Configuration (`config.yaml`)

```yaml
server:
  host: "0.0.0.0"          # Bind address
  port: 7870                # Port HTTP
  debug: true

storage:
  jobs_dir: "./jobs"        # Répertoire des données de traitement
  database_url: "sqlite:///transcrIA.db"   # prod : postgresql+psycopg://… (ou TRANSCRIA_DATABASE_URL)

auth:
  enabled: true
  first_admin_username: "admin"
  first_admin_password: "admin-change-me"

services:
  dashboard_llm_url: "http://127.0.0.1:5001"   # Monitoring GPU
  srt_editor_easy_url: "http://127.0.0.1:7861" # Éditeur SRT externe

models:
  stt_backend: "cohere"
  default_stt_model: "cohere-transcribe-03-2026"
  fallback_stt_model: "large-v3"
  cohere_model_path: "./models/cohere-asr/cohere-transcribe-03-2026"
  pyannote_model: "pyannote/speaker-diarization-community-1"


whisper:
  model_size: "large-v3"
  word_timestamps: true
  condition_on_previous_text: false
  collapse_repetition_loops: true
  forced_alignment:
    enabled: false
    backend: "torchaudio_ctc"

workflow:
  enable_quick_summary: true
  enable_speaker_detection: true
  enable_quality_mode: true
  enable_external_srt_editor_link: true
  audio_quality:
    force_quality_backend: true
    degraded_levels: ["degrade"]
  quality_transcription:
    force_stt_backend:
    enabled_for_modes: []
    force_on_degraded_summary: false
   vad:
     enabled_summary: true
     enabled_final: false
     auto_enable_final_on_degraded: true
     auto_enable_final_levels:
       - degrade
     threshold_final_degraded: 0.6
     adaptive: true
  speaker_realignment:
    enabled: true
  summary_llm:
    enabled: true
    model_id: "arbitrage"
    api_base: "http://127.0.0.1:8080/v1"
    timeout_seconds: 1800
    use_chat_api: true
  arbitration_llm:
    enabled: false
    model_id: "local/arbitrage"
    api_base: "http://127.0.0.1:8080/v1"
    timeout_seconds: 7200
    opencode_bin: "opencode"

security:
  retention_days: 365
  allow_job_delete: true
  allowed_upload_extensions: [".mp3", ".wav", ".m4a", ".mp4", ".flac", ".ogg"]

notifications:
  email:
    enabled: false
    smtp_host: "smtp.example.com"
    smtp_port: 587            # 587=STARTTLS, 465=SMTPS/SSL, 25=nu
    smtp_username: ""
    smtp_password: ""
    use_starttls: true        # true pour port 587
    use_ssl: false            # true pour port 465
    from_address: "transcria@example.com"
    from_name: "TranscrIA"
    base_url: "http://localhost:7870"  # URL publique pour les liens dans les emails
```

**Notes :**
- `config.example.yaml` (version template) diffère encore sur certains chemins/modèles, mais les timeouts LLM sont désormais calibrés pour des traitements longs
- Le vrai `config.yaml` du service peut monter plus haut sur `arbitration_llm.timeout_seconds` pour des réunions de plusieurs heures
- Les valeurs par défaut dans `_DEFAULT_CONFIG` sont plus basses : `summary_llm.timeout_seconds: 120`, `arbitration_llm.timeout_seconds: 600`. Les valeurs 1800/7200 ci-dessus sont des valeurs production adaptées aux réunions longues.


## 3.1 Pipeline STT qualité

Le backend normal reste `models.stt_backend` (`cohere` par défaut). Le mode `quality` active le workflow complet (diarisation, correction LLM, contrôles qualité), mais ne force plus Whisper par défaut. `PipelineService._config_for_mode()` ne change de backend que si `workflow.quality_transcription.force_stt_backend` est explicitement défini et que le mode demandé ou le diagnostic dégradé correspond aux règles configurées. La décision qualité est écrite dans `metadata/audio_quality_decision.json` et le backend réellement utilisé dans `metadata/transcription_metadata.json`.

Whisper large-v3 reste disponible pour les tests, fallbacks et campagnes ciblées. Il apporte les timestamps mot-à-mot, les seuils anti-hallucination de faster-whisper, la réduction de boucles répétitives (`anti_hallucination.py`), l'alignement CTC optionnel (`forced_alignment.py`) et le réalignement locuteur/ponctuation (`speaker_realignment.py`). Aucune dépendance WhisperX n'est utilisée.

Granite Speech 4.1 2B est intégré comme backend expérimental `granite`, désactivé par défaut. Il utilise `AutoProcessor` + `AutoModelForSpeechSeq2Seq`, le modèle local `models/granite-speech-4.1-2b/` si présent, des prompts IBM configurables et le flag `fix_mistral_regex=true` quand la version de `transformers` le supporte. La diarisation reste portée par pyannote : Granite normal produit seulement du texte par chunk, ensuite attribué aux tours pyannote comme Cohere.

**Fallback automatique Granite sur audio dégradé :** `PipelineService._config_for_mode()` bascule de `granite` vers le backend de production configuré dans `self.config` (ou `cohere` si celui-ci est aussi `granite`) quand `audio_quality_decision.json` indique `level=degrade` ou que `audio_preflight.json` contient le flag `audio_tres_faible`. Le backend de fallback effectivement utilisé est logué et tracé dans `metadata/transcription_metadata.json`.

**Parakeet TDT 0.6B v3** (`parakeet_transcriber.py`) est intégré comme backend expérimental `parakeet`, utilisant NeMo (`nemo_toolkit[asr]`). Il utilise `ASRModel.from_pretrained()` au lieu du pipeline Transformers `generate()`. Particularités vs les autres backends : auto-détection de langue (25 langues), ponctuation et timestamps natifs, `rel_pos_local_attn` pour l'audio long (jusqu'à 3h). NeMo ignore `device_map` → `ParakeetTranscriber.load()` appelle `torch.cuda.set_device()` avant chargement. Pas de word boosting possible (pas d'équivalent hotwords/biasing). Documenté dans `docs/PARAKEET_STT_INTEGRATION.md`.

**Anti-hallucination STT :** `anti_hallucination.py` fournit `collapse_repetition_loops()` utilisé par `WhisperTranscriber`, `CohereTranscriber` et `GraniteTranscriber`. Les paramètres sont les mêmes : `repetition_loop_min_repeats` (défaut 4) et `repetition_loop_max_phrase_words` (défaut 10), avec `collapse_repetition_loops` activé par défaut et `repetition_loop_keep_repeats` (défaut 2) contrôlant le nombre de répétitions conservées.

**Nettoyage post-STT (`transcription_cleanup`)** : après la transcription, `Transcriber._cleanup_transcription_segments()` applique un nettoyage déterministe configurable via `workflow.transcription_cleanup` :

- **Suppression d'artefacts** : les patterns de sous-titrage récurrents (`Sous-titrage ST' 501`, `FR 2021`, `Société Radio-Canada`, etc.) sont retirés des segments SRT. Les patterns sont configurables via `workflow.transcription_cleanup.subtitle_artifact_patterns` (liste de regex, défaut `[]` = utilisation des patterns intégrés) et `subtitle_artifact_words` (liste de phrases, défaut `[]` = utilisation des mots-clés intégrés). Les variantes tronquées (`-titrage`, `titrage fr`, `titrage st`) sont aussi filtrées.
- **Artefacts isolés** : les segments uniquement ponctuation/tiret sont retirés, ainsi que quelques tokens courts connus (`501` par défaut) seulement quand ils forment un segment autonome très court.
- **Retrait d'hallucinations évidentes** : les segments majoritairement non latins (arabe/CJK/cyrillique/coréen) et les phrases génériques isolées observées sur réunions françaises (`thank you`, `bye`, etc.) sont retirés avant génération SRT. Ce filtre est volontairement conservateur et désactivable (`remove_obvious_hallucinations`) ; il ne supprime pas tous les segments marqués `suspect/degrade`.
- **Fusion de micro-segments** : les segments courts (< seuil configurable, même locuteur, gap court) sont fusionnés avec le segment précédent pour réduire les artefacts de fragmentation. Configurable via `merge_short_segments` (défaut `true`), `short_segment_max_s` (défaut 0.45), `short_segment_max_words` (défaut 2), `merge_gap_s` (défaut 0.5), `merge_max_chars` (défaut 220).

Les opérations sont tracées dans les logs du pipeline (`removed_artifacts=N, removed_hallucinations=N, merged_short_segments=M`).

Le VAD Silero reste actif par défaut en résumé. `AdaptiveVADConfig` adapte les seuils à partir de `metadata/audio_quality_decision.json` sans modifier la configuration globale. La transcription finale a le VAD désactivé par défaut (`enabled_final=false`) et l'auto-activation sur audio dégradé également (`auto_enable_final_on_degraded=false`). Le VAD interne de Whisper (`vad_filter`) est désactivé par défaut. Voir `docs/VAD_OR_NOT.md` pour l'analyse complète et les recommandations par type de fichier.

Pyannote écrit maintenant `speakers/diarization_checkpoint.json` pour réutiliser les tours si l'audio et le modèle n'ont pas changé, et `speakers/speaker_embeddings.json` comme checkpoint acoustique par locuteur. Sur les longues réunions, `diarization.preload_audio=true` passe `preload=True` au pipeline pyannote pour éviter les crops/décodages répétés. Si `diarization.prepare_pcm_audio=true`, `DiarizationPcmPreparer` crée un cache `speakers/diarization_16k_mono.wav` réservé à pyannote, contrôlé par durée source/cible et documenté dans `speakers/diarization_audio.json`. **Placement GPU** : `diarization.device` accepte `"auto"`/`"cuda"` générique → résolu vers la **carte la plus libre** (≥ `gpu.pyannote_vram_mb`) au moment du chargement, via `squim_scorer.pick_device` (contourne le GPU occupé par le LLM d'arbitrage / le STT en multi-GPU) ; un index explicite `cuda:N` est respecté, repli CPU sinon. Recommandé en nœud de ressources multi-GPU (cf. `docs/CONFIG_REFERENCE.md`).

---

## 4. Modules détaillés

### 4.1 Authentification (`transcria/auth/`)

**`models.py`**
| Classe/Enum | Rôle |
|---|---|
| `Role` | Enum : `admin`, `manager`, `operator`, `viewer` |
| `GroupRole` | Enum : `member`, `group_admin` |
| `ROLE_HIERARCHY` | Niveaux : VIEWER=0 → ADMIN=3 |
| `User` | Modèle SQLAlchemy (hérite `UserMixin`) |
| `Group` | Groupe de partage de jobs |
| `GroupMembership` | Association User↔Group avec rôle de groupe |

| Propriété/Méthode | Description |
|---|---|
| `User.set_password(pw)` | Hash via werkzeug (`generate_password_hash`) |
| `User.check_password(pw)` | Vérification hash (`check_password_hash`) |
| `User.has_role(minimum)` | Vérifie si le rôle >= minimum via `ROLE_HIERARCHY` |
| `User.role_enum` | Conversion str → Role (fallback VIEWER sur ValueError) |
| `User.to_dict()` | Sérialisation JSON |

**`store.py`**
| Méthode | Description |
|---|---|
| `UserStore.create_user(username, password, ...)` | Création utilisateur |
| `UserStore.get_by_id(user_id)` | Recherche par UUID |
| `UserStore.get_by_username(username)` | Recherche par nom |
| `UserStore.list_users(active_only)` | Liste des utilisateurs |
| `UserStore.update_user(user_id, **kw)` | Mise à jour partielle (ignore `password_hash`) |
| `UserStore.change_password(user_id, pw)` | Changement mot de passe |
| `UserStore.deactivate_user(user_id)` | Désactivation |
| `UserStore.record_login(user)` | Enregistre `last_login` + commit centralisé |
| `UserStore.count_users()` | Compte les utilisateurs |
| `UserStore.ensure_admin(config)` | Crée l'admin initial si aucun utilisateur |

**`groups.py`**
| Méthode | Description |
|---|---|
| `GroupStore.create_group(name, description)` | Création d'un groupe par admin global |
| `GroupStore.list_for_admin(user)` | Liste tous les groupes pour admin global, sinon seulement les groupes administrés |
| `GroupStore.add_member(group_id, user_id, role)` | Ajoute ou met à jour un membre actif existant |
| `GroupStore.remove_member(group_id, user_id)` | Retire un membre |
| `GroupStore.users_share_group(user_a_id, user_b_id)` | Teste la visibilité croisée des jobs |
| `GroupStore.can_manage_group(user, group_id)` | Autorise admin global ou `group_admin` du groupe |

**`permissions.py`**
| Élément | Description |
|---|---|
| `Permission` | Enum : `CREATE_JOBS`, `VIEW_ALL_JOBS`, `DELETE_JOBS`, `MANAGE_USERS`, `MANAGE_CONFIG`, `ACCESS_SYSTEM`, `DOWNLOAD_EXPORTS`, `VIEW_QUALITY_REPORTS`, `RETRY_PROCESSING` |
| `_ROLE_PERMISSIONS` | Mapping Role → set[Permission] (ADMIN=tous, MANAGER=CREATE_JOBS+VIEW_ALL_JOBS+DOWNLOAD+QUALITY+RETRY, OPERATOR=CREATE_JOBS+DOWNLOAD+QUALITY, VIEWER=DOWNLOAD seul) |
| `get_user_permissions(user)` | Retourne les permissions pour un user (set vide si non authentifié) |
| `@requires(permission)` | Décorateur Flask : abort(401) si non authentifié, abort(403) si non autorisé |

**`routes.py`**
| Route | Méthode | Rôle requis |
|---|---|---|
| `/login` | GET, POST | Aucun |
| `/logout` | GET | Authentifié |
| `/account/password` | GET, POST | Authentifié |
| `/admin/users` | GET | `MANAGE_USERS` |
| `/admin/users/new` | GET, POST | `MANAGE_USERS` |
| `/admin/users/<id>/edit` | GET, POST | `MANAGE_USERS` |
| `/admin/groups` | GET | Admin global ou admin d'au moins un groupe |
| `/admin/groups/new` | GET, POST | `MANAGE_USERS` |
| `/admin/groups/<id>/edit` | GET, POST | Admin global ou `group_admin` du groupe |

`inject_user_context()` est un context processor Flask injectant `current_user`, `user_permissions` et `can_manage_groups` dans les templates.

**Gestion des mots de passe :** les utilisateurs authentifiés changent leur mot de passe via `/account/password`, avec vérification du mot de passe actuel, confirmation et minimum de 8 caractères. En cas d'oubli, le chemin prévu est le reset par un admin global dans `/admin/users/<id>/edit`; il n'y a pas de reset email tant qu'aucune configuration SMTP/tokens n'est définie.
Au premier démarrage, `UserStore.ensure_admin()` logue un warning si le compte admin initial est créé avec `admin-change-me`, `CHANGE-ME` ou un mot de passe vide.

**Règle de visibilité jobs par groupe :** un job reste propriété d'un utilisateur (`jobs.owner_id`). Les membres d'un même groupe voient les jobs des autres membres via `JobStore.list_for_user()` et `_can_access_job()`. Il n'y a pas encore de partage job par job ni de notion de groupe propriétaire.

---

### 4.2 Jobs (`transcria/jobs/`)

**`models.py`**
| Énumération | Valeurs (20 états) |
|---|---|
| `JobState` | `created`, `uploaded`, `analyzed`, `summary_running`, `summary_done`, `context_done`, `participants_done`, `lexicon_done`, `speaker_detection_running`, `speaker_detection_done`, `ready_to_process`, `transcribing`, `diarizing`, `arbitrating`, `quality_checking`, `quality_checked`, `export_ready`, `completed`, `failed`, `cancelled` |

| `WORKFLOW_STEPS` (9 étapes affichées) |
|---|
| `file` (Fichier), `analyze` (Analyse), `summary` (Résumé), `context` (Contexte), `participants` (Participants & Locuteurs), `lexicon` (Lexique), `processing` (Traitement), `quality` (Qualité), `export` (Export) |

> **Note :** `WORKFLOW_STEPS` dans `workflow/steps.py`, `WorkflowState.STEPS` et `get_step_for_state()` dans `jobs/models.py` sont alignés sur les mêmes 9 étapes affichées. Les locuteurs sont fusionnés dans l'étape 5 "Participants & Locuteurs".

| Classe | Colonnes |
|---|---|
| `Job` | `id` (UUID PK), `owner_id` (FK→users), `title`, `state`, `processing_mode`, `created_at`, `updated_at`, `extra_data_json`, `error_message` |

| Fonction | Description |
|---|---|
| `get_state_order(state)` | Index dans l'énumération JobState |
| `get_step_for_state(state)` | Retourne l'étape `WORKFLOW_STEPS` correspondante |

**`store.py`**
| Méthode | Description |
|---|---|
| `JobStore.create_job(owner_id, title)` | Création traitement |
| `JobStore.get_by_id(job_id)` | Recherche |
| `JobStore.list_for_user(user, include_all)` | ADMIN voit tout ; les autres voient leurs propres jobs et les jobs des membres des groupes partagés, sauf `include_all=True` |
| `JobStore.update_state(job_id, state, error)` | Transition d'état |
| `JobStore.update(job_id, **kw)` | Mise à jour partielle |
| `JobStore.delete_job(job_id)` | Suppression |
| `JobStore.count_jobs()` | Compte les jobs |

**`filesystem.py`**
| Classe | Méthodes |
|---|---|
| `JobFilesystem(jobs_dir, job_id)` | `save_json()`, `load_json()`, `save_text()`, `load_text()`, `save_upload()`, `get_original_audio_path()`, `cleanup()`. `save_json`/`save_text` écrivent de façon **atomique** (temp unique → `fsync` → `os.replace`) : aucun lecteur concurrent ne voit de fichier tronqué |

**`artifact_store.py`** — magasin de fichiers de jobs partagé via PostgreSQL (`storage.shared_backend: pg`, topologie split sans filesystem commun — `docs/STOCKAGE_PARTAGE_JOBS.md`)
| Fonction | Description |
|---|---|
| `push_job_files(cfg, job_id, prefixes=…)` | Pousse en base les fichiers locaux nouveaux/modifiés (upsert idempotent par sha256, transaction par fichier, chunks 8 Mo) |
| `pull_job_files(cfg, job_id, prefixes=…)` | Matérialise localement les fichiers de la base (tmp + sha256 vérifié + `os.replace`) ; n'écrase jamais un fichier local modifié non poussé (manifeste `.sync_state.json`) |
| `pull_job_files_throttled(cfg, job_id)` | Pull paresseux best-effort (hook `before_app_request`, au plus 1 pull/job/2 s) |
| `purge_input_files(cfg, job_id)` | Supprime les blobs `input/` (poids lourd) aux états terminaux du pipeline |
| `delete_job_files(job_id)` | Purge totale à la suppression du job (en plus du `ON DELETE CASCADE`) |
| `newest_synced_mtime_ns(cfg, job_id)` | Fraîcheur des artefacts locaux (test de péremption du package zip reconstruit) |

No-op intégral quand `shared_backend` vaut `fs` (défaut). Préfixes synchronisés : `input/`, `context/`, `metadata/`, `speakers/`, `quality/`, `summary/` ; exclus : `exports/` (reconstruit localement), `audio/` (intermédiaires), `metadata/audio_excerpts/` (cache à la demande).

Structure disque d'un job :
```
jobs/{uuid}/
├── input/original.{ext}
├── metadata/
│   ├── audio_analysis.json
│   ├── audio_excerpts/                 # Cache WAV des écoutes de contextes lexique
│   ├── transcription.srt
│   ├── transcription_corrigee.srt      # SRT corrigé par opencode
│   ├── transcription_segments.json
│   ├── stt_corpus.json                 # Corpus difficulté↔qualité par segment (calibration STT, cf. STT_ADAPTATIF_ET_HYBRIDE.md)
│   ├── speakers_map.json
│   └── correction_report.md
├── summary/
│   ├── quick_transcript.txt
│   ├── summary.json
│   ├── diarization_context.md
│   └── summary.md
├── context/
│   ├── meeting_context.json
│   ├── participants.json
│   ├── session_lexicon.json
│   ├── session_lexicon.txt
│   ├── job_context.yaml
│   └── job_context.json
├── speakers/
│   ├── speaker_turns.json
│   ├── speaker_stats.json
│   ├── speaker_mapping.json
│   ├── speaker_clips.json
│   └── samples/                         # Extraits WAV par locuteur
├── quality/
│   ├── quality_report.json
│   ├── quality_report.md
│   └── review_points.json
└── exports/
    └── transcrIA_job_{uuid}.zip
```

---

### 4.3 Workflow (`transcria/workflow/`)

**`states.py` — `WorkflowState`**
9 étapes affichées (pas 10) :

| # | id | label | route |
|---|---|---|---|
| 1 | file | Fichier | upload |
| 2 | analyze | Analyse | analyze |
| 3 | summary | Résumé | summary |
| 4 | context | Contexte | context |
| 5 | participants | Participants & Locuteurs | participants |
| 6 | lexicon | Lexique | lexicon |
| 7 | processing | Traitement | processing |
| 8 | quality | Qualité | quality |
| 9 | export | Export | export |

| Méthode | Description |
|---|---|
| `get_steps()` | Retourne les 9 étapes [{id, label, order, route}] |
| `compute_statuses(job_state)` | Calcule StepStatus pour chaque étape selon l'état du job |
| `get_next_step(statuses)` | Retourne la prochaine étape à faire (todo/in_progress/error) |

**`StepStatus`** : `todo`, `in_progress`, `done`, `optional`, `error`, `skipped`

**`steps.py` — `WorkflowSteps`**
Contient `WORKFLOW_STEPS` (9 entrées, sans étape `speakers` séparée) et des helpers :
- `step_requires_upload(step_id)` : retourne True pour file, analyze, summary, participants, processing, quality, export
- `step_requires_speakers(step_id)` : retourne True pour processing, quality
- `get_step_index(step_id)` / `get_next_step_id(step_id)`

**`progress.py` — `WorkflowProgressReporter`**
Persiste une progression utilisateur courte dans `jobs.extra_data_json["workflow_progress"]`.
Le wizard la lit via `GET /api/jobs/<id>/status` pour afficher une activité discrète
pendant les phases longues. Les écritures non forcées sont throttlées par
`workflow.progress.update_interval_s`; les messages doivent rester courts et non confidentiels.

**`runner.py` — `WorkflowRunner`**
| Méthode | Description | GPU |
|---|---|---|
| `run_analyze(job, audio_path)` | ffprobe | — |
| `run_summary(job, audio_path, config)` | Cohere transcription → pyannote si activé → opencode résumé. Réutilise le transcript en cache (`_load_cached_quick_summary`) pour éviter de refaire le STT à une relance. Saute la réservation VRAM locale quand `summary_stt` est servi à distance (`_phase_runs_remotely`). Matérialise au passage `summary/meeting_invite.md` depuis `extra_data["meeting_invite"]` (`_materialize_meeting_invite`). Sur VRAM insuffisante au STT rapide, restaure l'état pré-résumé et renvoie `{"vram_wait": True, ...}` — l'appelant met en attente + enfile la reprise serveur. Si la LLM ne produit rien après **3 tentatives** (`_run_llm_summary`), renvoie `{"summary_llm_failed": True}` : pas de `SUMMARY_DONE`, `meeting_context` non corrompu, relançable | GPUSession auto |
| `run_speaker_detection(job, audio_path, config, update_state=True)` | pyannote diarization + formatage via GPUSession. Applique d'abord `apply_speaker_hint(config, job.extra_data["speaker_hint"])`. `update_state=True` (détection manuelle) publie `SPEAKER_DETECTION_RUNNING/DONE/FAILED` ; `update_state=False` (sous-phase de `run_summary`) ne touche pas l'état (le job reste `SUMMARY_RUNNING`, diarisation best-effort) | GPUSession auto |
| `run_transcription(job, audio_path, config)` | Cohere ASR → segments → apply_speakers → SRT | GPUSession auto |
| `run_diarization(job, audio_path, config)` | pyannote speaker mapping via GPUSession. Applique aussi `apply_speaker_hint()` (même hint déterministe → checkpoint cohérent entre phases) | GPUSession auto |
| `run_correction(job, config)` | opencode + LLM d'arbitrage : correction speakers + application du lexique validé en contexte + orthographe (prompt v3.0, @general obligatoire) | LLM arbitrage |
| `run_final_review(job, config)` | **Phase de relecture finale (A+C+D+G)** après correction, avant qualité : harmonise la synthèse, rend cohérents noms/termes du SRT, résout les variantes, audite les données structurées (corrige nom/chiffre/date, marque `[À VÉRIFIER]`). Réutilise la LLM chargée. `_apply_final_review()` applique les sorties avec garde-fous (SRT relu si ratio 0.9–1.1, `summary_harmonized`, `structured_data` si JSON valide). **Best-effort** : renvoie toujours `success=True` | LLM arbitrage |
| `run_refine(job, config)` | **Chat d'affinage des livrables** (post-workflow, job terminé, tous profils) : chaque tour = entrée de file (mode `refine`), demande dans `refine/request.json`, historique `refine/chat.json`. `discuss` = réponse sans modification, **appel LLM direct** (`refine_llm.chat_completion`, `/v1/chat/completions`, thinking désactivé — une seule génération, pas d'opencode) ; `apply` = édition des artefacts texte via opencode dans un `AgentWorkspace`, garde-fous (`_corrected_srt_integrity_error`, JSON normalisé, `_sanitize_render_options`), **snapshot de version AVANT write-back** (`refine/versions/v<N>/` + manifeste, restauration via API), rebuild du package. Contexte conversationnel : les `context_turns` derniers tours sont rejoués à la LLM (vrais tours user/assistant en discuss) ; les points du contrôle qualité (`quality/review_points.json`, dont variantes lexique non résolues) sont fournis aux deux modes. **Best-effort** : tout échec → tour assistant explicatif, livrables intacts | LLM arbitrage |
| `run_quality_checks(job, config)` | Contrôle qualité **selon le profil** : complet (16 contrôles, `QualityReporter`) ou **léger** (`quality/light_report.py` : invariants SRT, schéma compatible) si `profile.run_quality == "light"` | — |
| `build_export(job, config)` | Package ZIP **gradué selon le profil** (`zip_level` minimal/standard/full, DOCX gaté par `docx_level`) | — |

Pipeline de traitement complet (`api_process`) — étapes **sélectionnées par le profil** :
```
run_transcription → cleanup_transcription → run_diarization (si profile.run_diarization) → run_correction (si profile.run_llm_correction) → run_final_review → run_quality_checks → build_export
```

Avec les 7 pré-traitements audio exécutés dans `_run_pipeline_steps()` avant `run_transcription()` :
```
_run_audio_preflight → _run_audio_scene_analysis → _run_source_separation → _run_audio_scene_filter → _run_audio_denoise → _run_audio_normalization → _run_transcription → cleanup → diarization → correction → quality → export
```

Étape 0 — `_run_audio_preflight()` : pré-diagnostic acoustique rapide (RMS, SNR estimé, bande passante, clipping, flags `audio_faible`/`audio_tres_faible`/`snr_faible`), sauvegarde `metadata/audio_preflight.json`. Les flags alimentent les étapes suivantes (normalisation auto, VAD dégradé, etc.).

`_run_audio_denoise()` — débruitage ffmpeg optionnel (filtre `afftdn`), désactivé par défaut (`workflow.audio_denoise.enabled=false`). Produit `denoised.wav` et écrit `metadata/audio_denoise.json` avec `preserve_timeline=true`.

Chaque étape de prétraitement est optionnelle et désactivée par défaut sauf `_run_audio_preflight()` (toujours actif) et `_run_audio_scene_analysis` (si `workflow.audio_scene.enabled=true`). `_run_audio_normalization()` inclut la détection auto-loudnorm (RMS < 0.02 → forçage loudnorm). `_cleanup_transcription_segments()` est activé par défaut (`workflow.transcription_cleanup.enabled=true`).

Cycle de vie GPU dans `run_summary` :
```
Phase 1: GPUSession(<backend>-summary, VRAM backend) → GPU auto → STT rapide → offload
Phase 1b: GPUSession(pyannote, 2 Go) → GPU auto → diarization → offload → diarization_context.md
           (extraits ≤200 chars/segment + section "Indices prénoms" : apostrophes directes & noms propres mid-phrase)
Phase 2: ensure_arbitrage_llm_ready(api_model_id) → opencode run
  (CAS A: réutilisation directe — CAS C: libération GPU + lancement)
  → LLM reste vivante pour la phase correction
```

Cycle de vie GPU dans `run_correction` :
```
ensure_arbitrage_llm_ready(api_model_id) → opencode run
  (CAS A garanti si run_summary vient de tourner — LLM déjà chargée et saine)
```

L'arrêt de la LLM est délégué à `PipelineService._release_arbitrage_llm()` via `finally` en fin de pipeline.

**`profiles.py` — profils de traitement** (remplacent le binaire `fast`/`quality`)
Contrat produit central et immuable : 6 profils nommés (`srt_express`, `srt_locuteurs`,
`word_rapide`, `word_structure`, `word_corrige`, `dossier_qualite`) + `legacy_fast` transitoire.
Chaque `ProcessingProfile` (frozen) déclare ses étapes humaines (`requires_*`), ses phases
machine (`run_diarization`, `run_llm_correction`, `run_quality`…), ses livrables
(`docx_level`, `zip_level`) et ses ressources (`ResourceRequirements`). Fonctions pures :
`get_profile`, `resolve_legacy_mode` (`fast→legacy_fast`, `quality→dossier_qualite`),
`resolve_request(profile_id|mode)`, `profile_for_job(job)` (lit le profil persisté dans
`extra_data.execution.processing_profile_id`), `profile_deliverables`, `profile_validations`.
Le cadrage complet et le plan en phases : `docs/PROFILS_TRAITEMENT_WORKFLOW.md`.

**`profile_availability.py`** — source unique de disponibilité pour le wizard :
`compute_profiles_view(config)` retourne, par profil, un statut structurel
(`available` / `available_remote` / `unavailable` / `disabled_by_config`) + raisons, livrables,
validations, et le **profil recommandé** (le plus élevé que la config/le matériel valide).
Exposé par `GET /api/profiles/availability` et injecté au rendu du wizard.

**Comment le profil traverse le système** (mode reste l'unité d'exécution legacy, le profil le
contrat produit) :
- *Routes* (`api_process`/`api_reprocess`) : `resolve_request()` → profil + mode de routage ;
  `processing_profile_id` persisté dans `extra_data.execution` (pas de colonne DB — choix
  transitoire réversible), audité (`processing_profile_id`/`queue_mode`/`legacy_mode`).
- *Scheduler/admission* : `PipelineService.estimate_profile_resources(config, profile)` construit
  le `vram_profile` à partir des phases réelles du profil → un profil sans LLM/diarisation n'expose
  pas la phase correspondante, donc l'admission ne le bloque jamais derrière (le scheduler durci
  est inchangé : il lit le `vram_profile` par job).
- *Pipeline* : `_resolve_profile(job, mode)` + `_define_pipeline_steps_for_profile()` sélectionnent
  les phases (parité stricte : `dossier_qualite` == ancien `quality`, `legacy_fast` == ancien `fast`).
- *Qualité/exports* : `run_quality_checks` et `PackageBuilder` lisent `profile_for_job(job)` ;
  job legacy / sans profil → comportement **complet** (compatibilité ascendante garantie).

> État : Phases 1-4, 6, 7 livrées. Restent la validation de charge mixte par profil (banc GPU),
> la granularité du *contenu* DOCX par niveau, la politique `api_quality` et des notifications
> profile-aware (cf. plan d'action du cadrage).

---

### 4.4 Audio (`transcria/audio/`)

**`analyzer.py` — `AudioAnalyzer`** (méthodes de classe)
| Méthode | Description |
|---|---|
| `analyze(file_path)` | Appelle ffprobe, retourne dict avec duration_seconds, codec, channels, sample_rate_hz, needs_conversion, estimated_machine_minutes, estimated_human_minutes, estimated_total_minutes, size_bytes, format |
| `_needs_conversion(info)` | Vérifie si codec ≠ PCM 16-bit LE, channels ≠ 1, sample_rate ≠ 16000 |
| `_estimate_time(info)` | Retourne (machine_min, human_min) — machine = (durée×0.35+130s)×1.25 avec marge 25%, humain = 5min par tranche de 30min |
| `_format_duration(seconds)` / `format_estimate(info)` | Formatage humain de l'estimation (`1h04`, `12min30s`) |

**`converter.py` — `AudioConverter`** (méthodes de classe)
| Méthode | Description |
|---|---|
| `convert_to_wav_mono_16k(input_path, output_path)` | ffmpeg → PCM 16kHz mono, timeout 300s |

**`vad.py` — `SileroVAD`**
| Méthode/Propriété | Description |
|---|---|
| `available` | Propriété : True si `faster_whisper` importable |
| `get_speech_timestamps(audio_path)` | Détecte les zones de parole via Silero (faster_whisper) |
| `build_speech_chunks(audio_path, max_chunk_s)` | Découpe l'audio en chunks VAD (fallback 30s si indisponible) |
| `_fallback_chunks(duration, chunk_s)` | Chunking 30s fixe de secours |

**`scene_analyzer.py` — `AudioSceneAnalyzer`**
| Méthode | Description |
|---|---|
| `__init__(config)` | Lit `workflow.audio_scene` |
| `analyze(audio_path)` | Lance le subprocess `_scene_analysis_worker` (librosa CPU) avec timeout ; retourne le dict JSON ou `{}` en cas d'erreur/désactivation |

**`_scene_analysis_worker.py`** — Worker subprocess pur (fonctions testables unitairement)
Fonctions principales : `_compute_stats`, `_compute_gender_stats`, `_compute_signals`, `_segments_to_dicts`, `_problem_segments`, `_frames_to_segments`, `_classify_scene_frames`, `_estimate_gender_for_speech`, `_analyze_audio`. Produit `scene_segments`, `problem_segments`, `gender_segments` horodatés et les ratios dans le JSON de sortie.

**`scene_filter.py` — `AudioSceneFilterService`**
| Méthode | Description |
|---|---|
| `should_filter(audio_scene, mode, config)` | Vérifie si le filtre doit s'appliquer selon `enabled_for_modes` et `problem_segments` |
| `filter(audio_path, output_path, audio_scene, mode, config)` | Met en silence les `problem_segments` ciblés via ffmpeg sans changer la durée ; retourne le chemin de sortie ou `audio_path` si erreur |

**`normalization.py` — `AudioNormalizationService`**
| Méthode | Description |
|---|---|
| `should_normalize(mode, config)` | Vérifie si la normalisation doit s'appliquer selon `enabled_for_modes` |
| `normalize(audio_path, output_path, mode, config)` | Applique `loudnorm` et high-pass optionnel via ffmpeg ; retourne le chemin de sortie ou `audio_path` si erreur |

**Auto-loudnorm** : `PipelineService._run_audio_normalization()` détecte si le RMS audio est inférieur à `auto_loudnorm_rms_threshold` (défaut 0.02). Si oui, `loudnorm` est forcé automatiquement même si `audio_normalization.enabled=false`. Le résultat est tracé dans `audio_normalization.json` avec `"forced": true, "reasons": ["audio_trop_silencieux_auto_loudnorm", "rms=..."]`.

**`source_separation.py`** — Séparation de sources vocales (Demucs, optionnel)
| Classe | Description |
|---|---|
| `SourceSeparationDecider` | `should_separate(analysis, quality, audio_scene)` — scoring pondéré sur signaux VAD/hallucinations/scène ; si `audio_scene.has_music=True` → séparation forcée **sauf si** `speech_ratio < scene_music_min_speech_ratio_for_force` (défaut 0.08), auquel cas la musique est ignorée comme faux positif sur parole quasi absente |
| `SourceSeparationService` | `separate(audio_path, output_path)` — extraction piste vocale Demucs (`htdemucs`) ; dégradation gracieuse si demucs absent |

---

### 4.5 STT (`transcria/stt/`)

**`base_transcriber.py` — `BaseTranscriber` (ABC)**
| Méthode/Propriété | Description |
|---|---|
| `available` | Propriété abstraite : True si le backend est utilisable |
| `load()` | Méthode abstraite : charge le modèle en VRAM |
| `transcribe(...)` | Méthode abstraite : transcription → segments `[{start, end, text}]` |
| `offload()` | Méthode abstraite : libère le modèle de la VRAM |
| `segments_to_srt(segments, speaker_map)` | Conversion segments → SRT standard avec préfixe speaker |

**`cohere_transcriber.py` — `CohereTranscriber`** (étend BaseTranscriber)
| Méthode/Propriété | Description |
|---|---|
| `__init__(model_path, device)` | Initialise avec chemin modèle et device (`cuda:0` par détection auto) |
| `available` | Vérifie torch + transformers importables |
| `load()` | Charge `CohereTranscribeModel` via `AutoModelForSpeechSeq2Seq` + `AutoProcessor` (trust_remote_code=True, bfloat16) |
| `transcribe(audio_path, audio_array, sample_rate, language, chunk_length_s, progress_callback)` | Si `audio_array` fourni → inférence directe sur numpy array ; sinon charge depuis `audio_path`. Segmentation en chunks de 30s → segments `[{start, end, text}]` |
| `segments_to_srt(segments, speaker_map)` | Conversion segments → SRT standard avec préfixe speaker |
| `offload()` | Libère modèle + processor + gc + cuda.empty_cache |
| `_seconds_to_srt_time(seconds)` | Conversion timestamp SRT (HH:MM:SS,mmm) |

Constantes : `_COHERE_MODEL_REPO = "CohereLabs/cohere-transcribe-03-2026"`, `_SUPPORTED_LANGUAGES` (14 langues)

> **Note :** `audio_array` + `sample_rate` permettent de passer un `np.ndarray` déjà en mémoire (chunking par tours pyannote), évitant les I/O disque.

**`whisper_transcriber.py` — `WhisperTranscriber`** (étend BaseTranscriber)
| Méthode/Propriété | Description |
|---|---|
| `available` | Vérifie `faster_whisper` importable |
| `load()` | Charge `faster_whisper.WhisperModel` sur device spécifié |
| `transcribe(audio_path, language)` | Transcription generator-based via faster-whisper → segments `[{start, end, text}]` |
| `offload()` | Libère modèle + gc + cuda.empty_cache |
| `available_sizes()` | Retourne les tailles de modèle disponibles |
| `vram_for_size(size)` | VRAM estimée par taille de modèle |

**`granite_transcriber.py` — `GraniteTranscriber`** (étend BaseTranscriber)
| Méthode/Propriété | Description |
|---|---|
| `available` | Vérifie `torch`, `transformers` et `transformers>=4.52.1` |
| `load()` | Charge `AutoProcessor` + `AutoModelForSpeechSeq2Seq` avec `trust_remote_code=True`, modèle local si disponible, `fix_mistral_regex` avec fallback logué |
| `transcribe(audio_path, audio_array, sample_rate, language)` | Charge l'audio ou utilise un `np.ndarray`, découpe selon `granite.chunk_length_s`, applique le prompt configuré et retourne des segments `[{start, end, text}]` |
| `get_metadata()` | Retourne les métadonnées sauvegardées dans `metadata/granite.json` |
| `offload()` | Libère modèle + processor + tokenizer + cache CUDA |

**`transcriber_factory.py` — `TranscriberFactory`**
| Méthode | Description |
|---|---|
| `create_transcriber(config, backend, device) -> BaseTranscriber` | Instancie le transcriber selon le backend demandé (`cohere`, `whisper` ou `granite`) |
| `list_available_backends()` | Liste les backends disponibles |
| `get_backend_vram_mb(backend)` | VRAM estimée pour un backend |

**`transcription.py` — `Transcriber`**

Deux modes de chunking :
- **Mode pyannote_turns (prioritaire)** : si `speaker_turns.json` contient `exclusive_turns`, charge l'audio une seule fois, découpe par tours pyannote → `np.ndarray` → `CohereTranscriber.transcribe(audio_array=...)`. Attribution speaker 100% fiable.
  - `_build_chunks_from_turns()` : découpe l'audio par tours exclusifs
  - `_apply_vad_filter()` : filtre les chunks VAD Silero
  - `_transcribe_by_chunks()` : transcrit chaque chunk avec le backend choisi
  - **Exception `audio_tres_faible`** : si le preflight détecte ce flag, le mode pyannote_turns est bypassé même si `exclusive_turns` est présent (pyannote ne détecte souvent qu'un tour ~5 s, limitant la transcription à ~17 % du signal). La cause est tracée dans `transcription_metadata.json` sous `chunking_forced_30s_reason`.
- **Mode 30s_fallback** : chunking 30s fixe + `_apply_speakers()` (overlap matching). Utilisé si `exclusive_turns` est absent, si pyannote est indisponible, ou si le flag `audio_tres_faible` force ce mode.

| Méthode | Description |
|---|---|
| `__init__(config, gpu_index)` | Initialise avec config et gpu_index |
| `transcribe(job, audio_path)` | Cohere → sauvegarde SRT + `segments.json` + `speakers_map.json` |
| `_apply_speakers(segments, speaker_turns, speaker_mapping)` | Overlap speaker-to-segment : pour chaque segment, trouve le turn avec le plus grand overlap |

> **Note :** `Transcriber.transcribe()` sauvegarde `metadata/speakers_map.json` avec `speaker_map = speaker_mapping or {}`.

**`base_diarizer.py` — `BaseDiarizer` (ABC)**
| Méthode | Description |
|---|---|
| `__init__(config, device)` | Stocke config et device ; crée un logger |
| `model_name` (abstractproperty) | Identifiant du modèle (string) |
| `available` (abstractproperty) | True si la dépendance du backend est importable |
| `diarize(job, audio_path)` (abstractmethod) | Retourne `{available, turns, exclusive_turns, speakers, stats}` |
| `offload()` | gc.collect() + cuda.empty_cache() — hérité par tous les backends |
| `_load_cached_result(fs, audio_path)` | Vérifie cache disque via fingerprint audio+modèle |
| `_save_cache_metadata(fs, audio_path, result)` | Écrit `diarization_checkpoint.json` |
| `_effective_speaker_params()` | Normalise les contraintes locuteurs effectives (`min_speakers`, `max_speakers`, `num_speakers`) pour l'inférence et le cache |
| `_effective_pipeline_params()` | Normalise `diarization.pipeline_params` en paramètres numériques pyannote, exclus du cache si absents |
| `_extract_clips(audio_path, turns, speakers, fs)` | Extrait des extraits WAV par locuteur (3 clips, 3-12s) |
| `_cache_speaker_embeddings(turns, audio_path, fs)` | Calcule et stocke les empreintes acoustiques par locuteur |
| `_acoustic_embedding(audio, sr)` | Empreinte acoustique légère (durée, RMS) sans modèle ML |
| `_audio_fingerprint(audio_path)` | Hash SHA-256 de l'audio pour le cache |
| `_load_audio_gpu(audio_path, device)` | torchaudio → resample 16kHz → tensor GPU |

**`diarization.py` — `DiarizerService(BaseDiarizer)`**
| Méthode | Description |
|---|---|
| `__init__(config, device)` | Appelle `super().__init__()` ; lit `models.pyannote_model` |
| `available` | Vérifie `pyannote.audio` importable |
| `diarize(job, audio_path)` | Prépare optionnellement un WAV PCM 16 kHz mono réservé à pyannote (`diarization.prepare_pcm_audio`) → charge pipeline pyannote → applique `diarization.pipeline_params` via `Pipeline.instantiate()` si configuré → applique `embedding_batch_size`/`segmentation_batch_size` → inférence avec `preload=True` si activé et hook de progression logué (`diarization.progress_log_*`) → turns + extraction `exclusive_speaker_diarization` dans `exclusive_turns` (fallback `AttributeError` → turns standard) → stats → sauvegarde |

**`diarization_pcm.py` — `DiarizationPcmPreparer`**
| Méthode | Description |
|---|---|
| `prepare(fs, source_path)` | Retourne l'audio original si l'option est désactivée ou si l'entrée est déjà PCM 16 kHz mono ; sinon crée/réutilise `speakers/diarization_16k_mono.wav` |
| `_cached_pcm_is_valid(...)` | Vérifie le fichier préparé via empreinte source, chemin cible et durée |
| `_duration_s(path)` | Mesure la durée via `AudioAnalyzer`/ffprobe |
| `_source_fingerprint(path)` | Hash chemin absolu + taille + mtime ns pour invalider le cache |

Le fichier PCM préparé ne remplace pas l'audio de référence du job : les clips, les
embeddings de cache et `diarization_checkpoint.json` continuent d'utiliser l'audio
original fourni à `diarize()`. Cela garde les artefacts utilisateur et l'invalidation
de cache alignés sur le fichier source.

Le cache `speakers/diarization_checkpoint.json` inclut le modèle, l'empreinte audio,
les contraintes locuteurs et les `pipeline_params` effectifs. Changer
`diarization.num_speakers`, `min_speakers`/`max_speakers` ou un seuil VBx invalide
donc le cache au lieu de réutiliser des tours produits avec une autre configuration.

Calibration pyannote 2026-06 sur fenêtres de réunion dense : le réglage de chunking
validé côté transcription est `workflow.pyannote_chunking.max_chunk_s=45` avec
`cohere.chunk_length_s=30`. Les seuils VBx `clustering.threshold=0.50/0.55/0.65`
n'ont pas amélioré le comptage en mode nombre inconnu ; `diarization.num_speakers`
reste le seul levier mesuré qui force un comptage parfait quand l'information est
connue.

**`sortformer_diarizer.py` — `SortformerDiarizer(BaseDiarizer)`**

Constantes de module : `_DEFAULT_MODEL_ID` (repo HF), `_DEFAULT_NEMO_FILE` (nom du fichier `.nemo` attendu), `_DEFAULT_LOCAL_DIR = "models/sortformer-4spk-v2.1"` (répertoire local par convention).

| Méthode | Description |
|---|---|
| `__init__(config, device)` | Appelle `super().__init__()` ; lit `sortformer.model_id` (défaut `_DEFAULT_MODEL_ID`) |
| `available` | Vérifie `nemo.collections.asr.models.SortformerEncLabelModel` importable |
| `diarize(job, audio_path)` | `torch.cuda.set_device()` → `_load_model()` → `model.diarize(str(audio_path), verbose=False)` → `_parse_sortformer_output()` → sauvegarde |
| `_load_model()` | Stratégie de chargement : si `model_id` contient `/` → `from_pretrained()` HF avec fallback sur `_find_nemo_file()` si HF échoue ; sinon → `_find_nemo_file()` + `restore_from()`. Retourne `None` si aucun fichier trouvé. |
| `_find_nemo_file(model_id)` | Cherche `*.nemo` dans l'ordre : (1) `model_id` lui-même si fichier `.nemo`, (2) dossier `model_id/*.nemo`, (3) cache HF `~/.cache/huggingface/hub/models--{namespace}--{repo}/snapshots/*/` (ne scanne que les sous-dossiers), (4) `_DEFAULT_LOCAL_DIR/*.nemo` avec vérification exacte de `_DEFAULT_NEMO_FILE` en priorité |
| `_parse_sortformer_output(lines)` | Convertit `["start end speaker_N"]` en `[{start, end, speaker, duration}]`, ignore les lignes vides/malformées/durée zéro, trie par timestamp |
| `_normalize_speaker_id(nemo_id)` | `"speaker_0"` → `"SPEAKER_00"` |
| `_parse_gpu_index(device)` | `"cuda:1"` → `1`, `"cpu"` → `None` |

**`speaker_detection.py` — `SpeakerDetector`**
| Méthode | Description |
|---|---|
| `__init__(config)` | Initialise |
| `detect(job, audio_path, device)` | Charge speaker_turns.json si disponible, génère les clips manquants si besoin, sinon lance diarization → formate speakers avec speaking_time, turn_count |
| `_clean_name(raw_name, speaker_id)` | Retire les métadonnées automatiques des noms de locuteurs proposés |
| `save_mapping(job_id, jobs_dir, mapping)` | Sauvegarde speaker_mapping.json + met à jour speaker_stats avec mapped_name/mapped_to/validation |

**`summary.py` — `SummaryGenerator`**
| Méthode | Description |
|---|---|
| `__init__(config)` | Extrait `summary_llm` de la config |
| `generate_quick_summary(job, audio_path, gpu_index)` | SileroVAD pré-transcription via `build_speech_chunks()` → transcrit chaque chunk VAD avec `audio_array` via Cohere → sauvegarde quick_transcript.txt et summary.json → retourne dict (`transcript_text`, `transcript_short`, `summary_text`, `segment_count`) |
| `_llm_summarize(transcript, fs)` | Appel `/v1/chat/completions` avec prompt système |

> **Note :** `_llm_summarize` n'est plus appelé directement dans le workflow. Le résumé LLM est géré par `WorkflowRunner.run_summary` via `OpenCodeRunner`.

---

### 4.6 Contexte (`transcria/context/`)

**`meeting_context.py` — `MeetingContextManager`**
| Méthode | Description |
|---|---|
| `get(job, jobs_dir)` | Charge le contexte ou retourne default_context() |
| `save(job, jobs_dir, context_data)` | Merge avec existing en préservant les champs LLM (summary_llm, title_suggere, structured_data, type_specific_data, etc.) |
| `auto_suggest(job, jobs_dir)` | Suggestions basées sur le résumé |
| `default_context()` | Valeurs par défaut (language="fr", meeting_type="Réunion interne", sensitivity="normal") |

`MEETING_TYPES` (18) : Réunion interne, Réunion projet, Réunion technique, Formation, Réunion médicale / santé, RH, Entretien, CSE, CSE extraordinaire, CODIR / COMEX, Réunion client, Point projet, Réunion de crise, Séminaire / atelier, Négociation, Entretien individuel, Podcast / média, Autre

`TYPE_SPECIFIC_FIELDS` : dict `{type → [{key, label, type}]}` définissant les champs supplémentaires affichés dans le wizard selon le type choisi (ex. CSE → président/secrétaire/quorum, Point projet → nom_projet/sprint). Source unique de vérité partagée par le JS du wizard, l'API de sauvegarde et le rendu DOCX. Les valeurs saisies sont stockées dans `meeting_context.json` → `type_specific_data` et injectées dans `job_context.yaml` (`meeting.type_specific`) pour la correction LLM.

**`participants.py` — `ParticipantsManager`**
| Méthode | Description |
|---|---|
| `get(job, jobs_dir)` | Charge la liste ou retourne [] |
| `save(job, jobs_dir, participants)` | Valide (strip, id auto, default) et sauvegarde |
| `default_participant()` | Retourne {id:"", name:"", function:"", service:"", role:"", is_animator:False, expected:True, comment:""} |

**`invite_parser.py` — brief d'invitation (fonctions pures)**
| Fonction | Description |
|---|---|
| `sanitize_invite(raw) → {brief, names}` | Parse une invitation collée. `names` : orthographe probable des participants dérivée des seules parties locales `prenom.nom` des e-mails (signal non ambigu ; exclut les boîtes de ressource type `MS118001-201`). `brief` : texte normalisé, **adresses e-mail retirées**, taille plafonnée. Générique (aucune donnée métier en dur), PII minimisée (e-mails jamais conservés). |
| `render_invite_markdown(parsed) → str` | Rend `{brief, names}` en Markdown (`## Noms probables` + contexte) pour la LLM ; chaîne vide si rien d'exploitable. |

Flux : route `POST /api/jobs/<id>/meeting-invite` (`api_meeting_invite`) → `sanitize_invite` → `extra_data["meeting_invite"]={brief, names}` → `WorkflowRunner._materialize_meeting_invite` écrit `summary/meeting_invite.md` → `OpenCodeRunner.run_summary(..., invite_path)`. Fichier hors liste blanche d'export (`package_builder`) : non inclus dans le ZIP.

**`lexicon.py` — `LexiconManager`**
| Méthode | Description |
|---|---|
| `get(job, jobs_dir)` | Charge le lexique ou retourne [] |
| `save(job, jobs_dir, terms)` | Valide (strip, id auto, defaults), conserve les métadonnées centralisées et sauvegarde JSON + .txt |
| `import_from_file(job, jobs_dir, content)` | Import CSV ou liste simple (# = commentaire) |
| `load_global_lexicon(config)` | Charge configs/lexique_metier.txt |

`LEXICON_CATEGORIES` : personne, organisation, service, application, projet, sigle, métier, technique, produit, statut, médical, lieu, règlement, finance, montant, processus, document, expression, langue, mot suspect
`LEXICON_PRIORITIES` : critique, importante, normale

**Lexiques centralisés**
| Module | Description |
|---|---|
| `central_lexicon_models.py` | Tables `group_lexicons` et `group_lexicon_entries` |
| `central_lexicon_store.py` | CRUD, import CSV/TXT, permissions admin/admin groupe, périmètre job→groupes, stats d'usage, synthèse de sensibilité et contrôles qualité |
| `central_lexicon_service.py` | Préfiltrage affichage avec raison de proposition, fusion central + LLM + session et filtrage par présence dans le SRT |
| `central_lexicon_routes.py` | Interface `/admin/lexicons`, stats, signaux RGPD/PSSI, import/export CSV et alertes qualité |
| `lexicon_audit.py` | Résumés d'audit sans contenu brut : compteurs, catégories, priorités, sources et noms propres probables |

**Types de réunion personnalisés** (`docs/TYPES_REUNION_PERSONNALISES.md`)
| Module | Description |
|---|---|
| `meeting_type_catalog.py` | SOURCE UNIQUE en données : charge `transcria/data/meeting_types.yaml` (18 intégrés, fail-loud) et porte `validate_type_definition` — le contrat d'entrée des types personnalisés ET de l'import communautaire (bornes anti-injection : hex stricts, badge ≤ 16, ≤ 6 `extract_fields` avec instructions ≤ 200 sans guillemets/backticks/accolades) |
| `meeting_type_models.py` | Table `meeting_type_templates` : fiche `definition_json` + portée `private/group/global` + logo binaire (colonnes séparées, jamais dans la fiche) |
| `meeting_type_store.py` | RBAC (tout utilisateur crée en privé ; un admin de groupe liste et partage les privés de ses membres ; global = admin), quotas, collisions nom/slug interdites avec les intégrés, logo re-encodé Pillow, export/import (§8 du cadrage : import → privé + inactif « à relire », activé par édition-enregistrement) |
| `meeting_type_routes.py` | Page `/meeting-types` + API `/api/meeting-types*` (CRUD, scope, logo, `preview.docx` sur données factices, export/import) |
| `meeting_type_prompts.py` | Placeholders du prompt de résumé (`{{TYPES_REUNION}}`, `{{INDICES_TYPES}}`, `{{CHAMPS_EXTRACTION_TYPE}}`) — substitués à la construction de l'instruction, no-op strict sans placeholder |

**Principe structurant** : la fiche du type choisi à l'étape 4 est **MATÉRIALISÉE dans le
job** (`meeting_context["custom_type"]`, + `context/type_logo.png`) — le rendu DOCX et le
worker ne résolvent jamais un template en base (pas d'ambiguïté entre deux privés
homonymes, robuste en topologie split, suppression du template sans casse). Le rendu est
un **registre de sections ordonnées** : `render_options.order` (par job, chat d'affinage)
> `sections.order` de la fiche > ordre historique ; `contexte`/`pv` sont déplaçables mais
jamais supprimables (« une donnée extraite n'est jamais cachée »).

Le pré-remplissage de l'étape 6 utilise les lexiques globaux et les lexiques des groupes du propriétaire du job. `context/selected_lexicons.json` mémorise les lexiques cochés pour le job ; absent, tous les lexiques accessibles sont sélectionnés. `prefilter_lexicon_entries_for_display()` masque avant affichage les entrées centrales normales sans occurrence dans le transcript/résumé, tout en conservant les priorités `critique`/`importante`. Il ajoute `_display_reason` (`term_presence`, `variant_presence`, `priority`) pour expliquer dans l'UI pourquoi un terme est proposé. Une session déjà sauvegardée reste prioritaire et n'est pas écrasée. Avant correction, `WorkflowRunner.run_correction()` écrit `context/session_lexicon_filtered.json` : termes présents dans le SRT par forme ou variante, plus entrées `critique`/`importante` conservées en préservation.

Traçabilité RGPD/PSSI : les routes lexiques journalisent création, modification, suppression, ajout/modification/suppression d'entrée, import, export CSV, changement de périmètre et rattachement au job. L'export CSV est volontairement déclenché en `POST` et peut être réservé aux admins globaux avec `security.lexicon_export_admin_only=true`. `details_json` ne contient jamais les termes, variantes ou commentaires en clair ; il contient uniquement volumes, catégories, priorités, sources, groupe/job et signaux `contains_probable_person_names`.

Si `whisper.lexicon_hotwords.enabled=true` et que le backend STT effectif est Whisper, `PipelineService._inject_whisper_lexicon_hotwords()` lit `context/session_lexicon.json`, construit une liste bornée de hotwords avec `stt.lexicon_hotwords.build_whisper_hotwords()`, enrichit `effective_config["whisper"]["hotwords"]`, sauvegarde `metadata/whisper_hotwords.json` et logue candidats/injectés/exclus.

Si `cohere.lexicon_biasing.enabled=true` et que le backend STT effectif est Cohere, `PipelineService._inject_cohere_lexicon_biasing()` sélectionne uniquement les formes cibles validées du lexique de session, sauvegarde `metadata/cohere_lexicon_biasing.json`, puis `CohereTranscriber` construit un `TrieContextualBiasProcessor` depuis le tokenizer Cohere. Le processeur ajoute un bonus léger aux premiers tokens possibles (`start_boost`), puis un bonus plus fort aux tokens qui prolongent un terme déjà amorcé dans un beam (`boost`) ; il ne booste pas les variantes fautives. L'option reste expérimentale et désactivée par défaut.

**`job_context_builder.py` — `JobContextBuilder`**
| Méthode | Description |
|---|---|
| `build(job, jobs_dir)` | Agrège meeting_context + participants + speaker_mapping + session_lexicon → YAML + JSON |

Génère `context/job_context.yaml` et `context/job_context.json` avec : job_id, owner_user_id, generated_at, meeting, participants, speakers, lexicon, processing.

---

### 4.7 Qualité (`transcria/quality/`)

**`srt_checks.py` — `SRTChecker`**
| Méthode | Description |
|---|---|
| `check_segment(segment)` | Vérifie : vide, trop court (<0.1s), trop long (>120s), timestamps inversés |
| `check_segments(segments)` | Vérifie tous les segments, retourne {total, issues, clean_count} |

**`lexicon_checks.py` — `LexiconChecker`**
| Méthode | Description |
|---|---|
| `check(text, lexicon)` | Retourne {found, missing, variants_found} — comparaison insensible à la casse |

**`quality_report.py` — `QualityReporter`**
16 contrôles systématiques :
1. Segments vides (texte vide)
2. Segments très courts (< 0.5s)
3. Segments très longs (> 60s)
4. Trous temporels (> 5s)
5. Chevauchements (end > next start)
6. Locuteurs non mappés (SPEAKER_XX)
7. Termes normalisés du lexique absents (`replace_by`) dans le SRT corrigé
7bis. Variantes de lexique non résolues (formes exactes et proches après correction)
7ter. Garde-fous déterministes : noms de locuteurs modifiés (`speaker_name_violations`), segments marqués étrangers (`foreign_segments`), segments non latins (`non_latin_segments`), segments courts suspects de bruit ASR (`suspicious_short_segments`) — ces quatre sous-checks alimentent le `review_load`. Un segment court n'est compté comme *probable hallucination* (`corroborated_count`) que s'il est corroboré par un signal indépendant : recoupement d'une `problem_segment` audio (`_segment_overlaps_zones`), `no_speech_prob` élevé, ou faible confiance des mots. Sans corroboration, c'est une interjection brève (sévérité `info`). Un nombre dicté (« 1,26 ») n'est jamais classé bruit.
8bis. Flags du pré-diagnostic acoustique (`audio_preflight_flags`) — `audio_faible`, `audio_tres_faible`, `snr_faible`, etc.
9. Segments suspects : `no_speech_prob` élevé (hallucination Whisper sur silence/audio dégradé)
10. Segments suspects : faible confiance mots (`suspect_low_word_confidence`) — ratio de mots à faible probabilité > seuil
11. Fiabilité segmentaire post-STT (`segment_reliability`) — classification ok/suspect/degrade par segment via `SegmentReliabilityScorer`
12. Couverture audio (< 80%)
13. Ratio mots/seconde suspect (< 0.5 ou > 10)

Score (`compute_quality_score`, fonction pure) — reflète la **fiabilité de la transcription**, pas le volume de points à vérifier :
- base = ratio de fiabilité segmentaire (`ok` plein, `suspect` à moitié, `degrade` nul), normalisé par le nombre de segments pour rester comparable d'une réunion de 5 min à 2 h ;
- la couverture audio ne pénalise qu'**en dessous** du seuil configuré (les silences normaux ne font pas chuter le score) ;
- déductions plafonnées et pondérées par gravité pour les **erreurs avérées** : noms de locuteurs altérés (≤20), hallucinations non latines (≤15), segments étrangers/vides/variantes lexique non résolues (≤10), termes lexique manquants (≤5).

Les signaux purement contextuels (silences, interjections courtes, chevauchements non significatifs) restent dans les `review_points` mais ne touchent jamais le score. Le compteur `warnings` (« Points d'attention ») reste un décompte de relecture distinct du score. Sauvegarde quality_report.json, quality_report.md, review_points.json.

**`review_points.py` — `ReviewPoints`**
| Méthode | Description |
|---|---|
| `generate(quality_report)` | Traduit les checks en phrases utilisateur |

---

### 4.8 Exports (`transcria/exports/`)

**`package_builder.py` — `PackageBuilder`**
| Méthode | Description |
|---|---|
| `build_package(job)` | Crée `transcrIA_job_{uuid}.zip` avec tous les fichiers + rapport DOCX |

Contenu du ZIP :
```
audio/original.{ext}
subtitles/transcription.srt              # SRT corrigé si disponible, sinon original
subtitles/transcription_segments.json     # si disponible
context/job_context.yaml, meeting_context.json, participants.json, session_lexicon.json
context/speaker_mapping.json, speaker_stats.json
quality/quality_report.md, quality_report.json, review_points.json
quality/correction_report.md             # si disponible
rapport_<titre>.docx                     # rapport Word généré automatiquement
```

**`docx_report.py` — `DocxReport` / `generate_docx_report()`**

Génère un rapport Word professionnel **adapté au type de réunion** à partir des artefacts JSON d'un job terminé. Endpoint : `GET /api/jobs/<id>/download/docx`. Mis en cache dans `exports/rapport_<titre>.docx` et inclus automatiquement dans le ZIP. Module exclu de mypy (python-docx n'a pas de stubs).

**Rendu Markdown** : le résumé LLM est du Markdown. `_split_markdown_bold(text)` (fonction pure) découpe le texte en segments `(contenu, gras)` selon `**…**`/`__…__` ; `_add_markdown_runs(paragraph, text)` les ajoute en runs **gras réels**. La Synthèse rend en plus les intertitres (`##` → ligne en gras détachée), les puces (`-`/`*`) et un espacement de paragraphe — corrige le gras non rendu et l'effet « tassé » (auparavant les astérisques étaient simplement supprimées et les lignes vides ignorées).

| Section | Source | Condition |
|---|---|---|
| Page de garde (bannière+badge selon thème, titre, métadonnées, quorum CSE) | `meeting_context.json` + `quality_report.json` | Toujours |
| Contexte (sujet, objectif, synthèse validée) | `meeting_context.json` → champ `summary` | Toujours |
| Champs type-spécifiques (président CSE, nom projet…) | `meeting_context.json` → `type_specific_data` | Si champs remplis |
| Sections enrichies (décisions, actions, votes, résolutions, ODJ…) | `meeting_context.json` → `structured_data` | Selon type + données non vides |
| Participants (nom, fonction, temps de parole %) | `participants.json` + `speaker_stats.json` | Toujours |
| Transcription (timestamp, locuteur, texte) | `metadata/transcription_corrigee.srt` | Toujours |
| Points à vérifier (conditionnel) | `quality_report.json` — coverage faible, zones audio, termes | Si flags qualité |

**Adaptation au type — trois couches** :
1. **Extraction structurée** : le résumé LLM (prompt section 8b) produit un bloc JSON `structured_data` parsé par `OpenCodeRunner._parse_structured_data()` avec 3 niveaux de repli (`ok`/`partial`/`failed`/`missing`) ; échec → rapport standard sans crash.
2. **Affichage des sections enrichies** : règle « une donnée extraite n'est jamais cachée ». Toute section (`points_odj`, `decisions`, `votes`, `resolutions`, `actions`, `blocages`, `reports`) s'affiche dès qu'elle est non vide, **quel que soit le type** — le type ne filtre pas la rétention du contenu. Ordre fixe type-PV (agenda → décisions → votes → résolutions → actions → blocages → reports). Numérotation dynamique des sections. *(Décision validée par un run réel sur conseil municipal : des votes extraits ne doivent pas être jetés faute de type CSE.)*
3. **Thèmes visuels** : `_DocxTheme` (dataclass) + `_THEMES` dict associent une palette (primary/accent/light), une bannière et un badge à chaque type. `_get_theme(meeting_type)` retourne le thème ou `_THEME_DEFAULT`. `_CSE_TYPES` pilote le quorum + sous-titre objet de séance sur la page de garde ; `_AUTO_CONFIDENTIEL` (Entretien individuel/RH/Médical) force la confidentialité.

Spec détaillée : `docs/archive/FEATURE_DOCX_REPORT.md` (archive locale, non publiée).

**Logique de priorité SRT** : `transcription_corrigee.srt` est servi en priorité. Si absent, `transcription.srt` est servi.

---

### 4.9 Intégrations (`transcria/integrations/`)

**`dashboard_client.py` — `DashboardClient`**
| Méthode | Endpoint | Description |
|---|---|---|
| `get_metrics()` | `GET /api/v1/metrics` | CPU, RAM, GPUs, services |
| `get_gpus()` | `GET /api/v1/gpus` | GPUs détaillés |
| `get_services()` | `GET /api/v1/services` | État des services |
| `get_gpu_processes()` | `GET /api/v1/gpus/processes` | Processus GPU |
| `get_system_status()` | Agrégation de tous les endpoints | État complet |

Tous les appels sont en try/except avec log debug si le dashboard est indisponible.

**`srt_editor_link.py` — `SrtEditorLink`**
| Méthode | Description |
|---|---|
| `push_audio(audio_path, filename)` | POST /api/upload/audio → SRT Editor |
| `push_srt(project_id, srt_content)` | POST /api/upload/srt → SRT Editor |
| `get_server_url(config)` | Retourne l'URL depuis la config |
| `resolve_public_url(config, request_host)` | Remplace 127.0.0.1 par l'IP réelle du host |

---

### 4.10 GPU (`transcria/gpu/`)

**`vram_manager.py` — `VRAMManager`**

Les valeurs clés sont lues depuis `config.yaml` :
- `services.arbitrage_script` (défaut `./scripts/launch_arbitrage.sh`)
- `services.stop_script` (défaut `./scripts/stop_arbitrage_llm.sh`)
- `services.arbitrage_log_path` (défaut `/tmp/arbitrage_llm_<port>.log`) — capture stdout+stderr du lancement
- `services.arbitrage_llm_port` (8080), `services.llm_cleanup_ports` (`[8000]`)
- **endpoint d'arbitrage** (hôte + port) résolu par `opencode_setup.resolve_arbitrage_endpoint` — **source unique** partagée avec `provision_opencode` (env `TRANSCRIA_ARBITRAGE_LLM_HOST` > `services.arbitrage_llm_host` > `127.0.0.1`). Un hôte **distant** (`_is_remote_arbitrage()`) ⇒ la LLM est **consommée seulement** : sonde HTTP `/v1/models`, jamais de launch/stop local (CAS C désactivé).
- `gpu.cohere_vram_mb`, `gpu.pyannote_vram_mb`, `gpu.llm_vram_mb`, `gpu.min_free_vram_mb`

| Méthode | Description |
|---|---|
| `get_gpu_info()` | Dashboard API ou fallback PyTorch, avec remapping `CUDA_VISIBLE_DEVICES` avant usage modèle |
| `get_free_vram_mb(gpu_index)` | VRAM libre en Mo pour l'ordinal CUDA visible |
| `get_best_gpu(required_mb)` | Meilleur GPU visible disponible (≥ required + MIN_FREE) |
| `ensure_free(required_mb, preferred_gpu)` | Scanne les GPUs visibles si le GPU courant est insuffisant → sélectionne le meilleur → log scan complet |
| `is_arbitrage_llm_running()` | Retourne True si l'API OpenAI-compatible répond (`/v1/models` + inférence test), avec fallback port/PID uniquement si nécessaire |
| `ensure_arbitrage_llm_ready(expected_model_id)` | Point d'entrée unique avant usage LLM : CAS A réutilisation, CAS B mauvais modèle, CAS C lancement — chaque chemin logué explicitement |
| `launch_arbitrage_llm()` | Lance `services.arbitrage_script` (sortie capturée dans `arbitrage_log_path`) → attend port (timeout 600s) → en cas d'échec, logue en `ERROR` le code de sortie + les dernières lignes du log |
| `stop_arbitrage_llm()` | Arrête la LLM d'arbitrage via `services.stop_script`, puis libère `arbitrage_llm_port` en fallback |
| `stop_cleanup_llm_ports()` | Libère les ports `services.llm_cleanup_ports` (vLLM, SGLang, llama.cpp, ik_llama.cpp ou autre backend concurrent) |
| `free_all_gpus()` | stop_cleanup_llm_ports + stop_arbitrage_llm + offload_all (reset forcé uniquement) |
| `is_port_open(port)` | Vérifie `/v1/models` accessible + teste une inférence réelle `/v1/completions` |
| `_log_all_gpus(label)` | Logue VRAM libre/totale/utilisée de chaque GPU (utilisé par ensure_free lors d'un basculement) |
| `_wait_for_port(port, timeout, *, proc, log_path)` | Boucle d'attente (`is_port_open` toutes les 5s) ; si `proc` est fourni, détecte sa mort précoce (`proc.poll()`) et abandonne sans attendre le timeout |
| `_diagnostic_tail(log_path)` | Dernières lignes du log de lancement, pour expliquer une panne dans les logs `ERROR` |
| `track_model / untrack_model` | Enregistre/désenregistre un modèle chargé |
| `offload_all()` | Vide `_loaded_models` + gc + cuda.empty_cache |

`VRAMManager` et `GPUAllocator` partagent la même logique `CUDA_VISIBLE_DEVICES` : un dashboard qui remonte des ids physiques (`0,2`) est converti vers les ordinaux visibles (`cuda:0`, `cuda:1`), tandis que le fallback PyTorch est marqué comme déjà remappé. `CUDA_VISIBLE_DEVICES=-1` masque tous les GPUs. La libération VRAM ciblée utilise `nvidia-smi -i <gpu physique>` et ne signale que les processus dont le nom correspond à `workflow.scheduling.kill_patterns`.

**`opencode_runner.py` — `OpenCodeRunner`**

**`llm_backend.py` — `LLMBackend` + implémentations**

| Classe | Description |
|---|---|
| `LLMBackend(config, port)` | Classe abstraite : `backend_type`, `base_url`, `model_id`, `is_available()`, `ensure_available()`, `shutdown()` |
| `ScriptLLMBackend` | Lancement via script shell (`launch_arbitrage.sh`), arrêt via stop script |
| `OllamaLLMBackend` | Lancement/arrêt via `ollama serve` et API native |
| `HTTPLLMBackend` | Connexion à une LLM externe déjà disponible (HTTP, pas de lancement local) |
| `create_llm_backend(config, backend_type)` | Factory : instancie le bon backend selon `config` et `backend_type` |

Le binaire opencode vient de `workflow.arbitration_llm.opencode_bin` ou de `TRANSCRIA_OPENCODE_BIN`. Le modèle vient de `workflow.summary_llm.model_id` pour le résumé et de `workflow.arbitration_llm.model_id` pour la correction. Si le `model_id` est vide, `OpenCodeRunner` lève `ValueError`.

| Méthode | Description |
|---|---|
| `__init__(work_dir, model, provider, opencode_bin, config)` | Initialise avec répertoire de travail, modèle et binaire opencode configurables |
| `run(instruction, prompt_file, timeout)` | Lance `opencode run --format json --model {provider}/{model}` via `subprocess.Popen` → parse NDJSON → retourne {success, output, files, events_count, tool_calls} |
| `run_summary(transcript_path, context_path, diarization_context_path, invite_path=None)` | Génère un résumé structuré via opencode + LLM d'arbitrage. Inclut la diarization acoustique si disponible et, si `invite_path` pointe vers un fichier existant, ajoute une clause d'instruction marquant le brief d'invitation comme **indicatif** (orthographe des noms / rôles / ordre du jour, sans forcer de correspondance 1:1). Vérifie qu'opencode a **réellement (ré)écrit** `summary.md` (mtime avant/après) ou émis du texte → expose `_summary_produced` ; le placeholder de `SummaryGenerator` n'est jamais parsé comme résumé |
| `run_correction(srt_path, context_path, lexicon_path)` | Correction SRT : lit transcription.srt + job_context.yaml + lexique filtré, écrit transcription_corrigee.srt + correction_report.md |
| `run_final_review(srt_path, summary_path, glossary_path, structured_data_path)` | Relecture finale en une session (prompt dédié `final_review_prompt.txt`, @general obligatoire pour le SRT) : A harmonise la synthèse, C+D fiabilisent cohérence/variantes du SRT, G audite les données structurées. Lit `summary_harmonized.md`, `transcription_reviewed.srt`, `structured_data_reviewed.json`, `final_review_report.md`. Glossaire bâti par `build_harmonization_glossary(participants, lexicon)` (fonction pure : noms validés + formes canoniques ← variantes) |
| `_parse_structured_summary(text)` | Parse le markdown LLM en dictionnaire avec regex (title_suggere, type_suggere, sujet_suggere, objectif_suggere, notes_suggeres, participants_detectes, mots_cles, speaker_count, termes_suspects). Applique `_strip_role_gender()` sur chaque ligne `## Participants probables` pour retirer un genre (Masculin/Féminin, ♂/♀) que la LLM aurait recopié dans le rôle (le genre a un champ dédié) |

**Fichiers prompts** dans `configs/prompts/` :
- `summary_prompt.txt` : Prompt système pour le résumé structuré
- `correction_prompt.txt` : Prompt pour la correction SRT (speakers + lexique + orthographe)

---

### 4.11 Web (`transcria/web/`)

**`routes.py` — Routes**

Le fichier contient les routes pages + API. Les routes liées aux jobs passent par `_require_job_access()` ou `_get_job_for_api()` pour vérifier que l'utilisateur est propriétaire du job ou admin.

| Route | Méthode | Auth | Description |
|---|---|---|---|
| `/health` | GET | Publique | Statut service + base de données |
| `/ready` | GET | Publique | Préparation du worker interne |
| `/metrics` | GET | Publique | Métriques Prometheus (`transcria_up`, `transcria_jobs_total`, `transcria_jobs_state`, `transcria_queue_entries`) |
| `/` | GET | login_required | Accueil (liste des traitements) |
| `/jobs/new` | POST | login_required + CREATE_JOBS | Création traitement |
| `/jobs/<id>` | GET | login_required + owner check | Assistant wizard 9 étapes |
| `/jobs/<id>/result` | GET | login_required + owner check | Page résultat |
| `/jobs/<id>/delete` | POST | login_required + DELETE_JOBS | Suppression traitement |
| `/system` | GET | login_required + ACCESS_SYSTEM | État technique (GPU dashboard) |
| `/admin/config` | GET, POST | login_required + MANAGE_CONFIG | Édition de la configuration : formulaires des réglages courants (onglet Réglages) + YAML complet (onglet avancé) |
| `/admin/queue` | GET | admin global ou admin de groupe | Vue de la file persistante et actions par job |
| `/admin/schedule` | GET | MANAGE_SCHEDULE | Gestion des créneaux de planification |
| `/admin/voices/consent-form.pdf` | GET | admin ou admin groupe | Formulaire PDF vierge de consentement vocal |
| `/admin/voices/<subject_id>/metadata` | POST | admin ou admin groupe autorisé | Mise à jour nom, genre validé, email et référence interne |
| `/admin/voices/<subject_id>/consent-proof/<consent_id>` | GET | admin ou admin groupe autorisé | Consultation de la preuve signée stockée sous `voices/` |
| `/admin/lexicons` | GET | admin ou admin groupe | Liste des lexiques centralisés administrables |
| `/admin/lexicons/new` | GET, POST | admin ou admin groupe | Création d'un lexique global ou de groupe selon droits |
| `/admin/lexicons/<id>` | GET | admin ou admin groupe autorisé | Détail, ajout, import et édition des entrées |
| `/api/jobs/<id>/upload` | POST | login_required + owner/admin check | Upload fichier audio |
| `/api/jobs/<id>/analyze` | POST | login_required + owner/admin check | Analyse ffprobe |
| `/api/jobs/<id>/summary` | POST | login_required + owner/admin check | Résumé rapide |
| `/api/jobs/<id>/context` | POST | login_required + owner/admin check | Sauvegarde contexte |
| `/api/jobs/<id>/participants` | POST | login_required + owner/admin check | Sauvegarde participants |
| `/api/jobs/<id>/lexicon` | POST | login_required + owner/admin check | Sauvegarde lexique |
| `/api/jobs/<id>/available-lexicons` | GET | login_required + owner/admin check | Lexiques centralisés accessibles au job |
| `/api/jobs/<id>/selected-lexicons` | POST | login_required + owner/admin check | Sauvegarde les lexiques cochés pour le préremplissage du job |
| `/api/jobs/<id>/audio/excerpt` | GET | login_required + owner check | Extrait WAV temporisé pour valider un contexte de lexique, audité comme `job_download` |
| `/api/jobs/<id>/speakers/detect` | POST | login_required + owner/admin check | Détection locuteurs |
| `/api/jobs/<id>/speakers/voice-match` | POST | login_required + owner/admin check | Suggestions depuis les voix enregistrées accessibles au job |
| `/api/jobs/<id>/speakers/map` | POST | login_required + owner/admin check | Mapping SPEAKER_XX |
| `/api/jobs/<id>/speakers/clips` | GET | login_required + owner check | Liste extraits audio |
| `/api/jobs/<id>/speakers/clip/<name>` | GET | login_required + owner check | Fichier WAV d'un extrait, audité comme `job_download` |
| `/api/jobs/<id>/process` | POST | login_required + owner/admin check | Traitement complet |
| `/api/jobs/<id>/quality` | POST | login_required + owner/admin check | Rapport qualité |
| `/api/jobs/<id>/export` | POST | login_required + owner/admin check | Construction package |
| `/api/jobs/<id>/download/srt` | GET | login_required + owner check | Téléchargement SRT |
| `/api/jobs/<id>/download/package` | GET | login_required + owner check | Téléchargement ZIP |
| `/api/jobs/<id>/download/audio` | GET | login_required + owner check | Téléchargement audio |
| `/api/jobs/<id>/push-to-editor` | POST | login_required + owner/admin check | Envoi vers SRT Editor EASY, audité comme `job_external_push` |
| `/api/jobs/<id>/lexicon/debug` | GET | login_required + owner check | Diagnostic détaillé du lexique : `audio_available`, timecodes bruts/normalisés, notes de réparation par contexte |
| `/api/jobs/<id>/status` | GET | login_required + owner check | Statut job JSON (polling) |
| `/api/jobs/<id>/reprocess` | POST | login_required + owner/admin check | Relance le traitement |
| `/api/jobs/<id>/refine` | POST | login_required + owner check | Soumet un tour du chat d'affinage (`kind` ∈ `discuss`/`apply`) : écrit `refine/request.json` puis enfile en mode `refine` (202 ; 409 si occupé ou job non terminé) |
| `/api/jobs/<id>/refine/chat` | GET | login_required + owner check | Endpoint de polling unique du panneau : tours, `busy`, versions, options de rendu et thèmes |
| `/api/jobs/<id>/refine/render-options` | POST | login_required + owner check | Options de rendu du rapport (thème, sections) — déterministe, SANS LLM, instantané |
| `/api/jobs/<id>/refine/revert` | POST | login_required + owner check | Restaure un snapshot `refine/versions/v<N>/` (les fichiers créés par l'apply sont supprimés) |
| `/meeting-types` | GET | login_required | Page « Types de réunion » (galerie + éditeur) |
| `/api/meeting-types` | GET, POST | login_required | Catalogue (intégrés + personnalisés visibles) / création d'un type PRIVÉ |
| `/api/meeting-types/<id>` | PUT, DELETE | créateur ou admin de portée | Édition (active un import « à relire ») / suppression (les jobs passés gardent leur fiche matérialisée) |
| `/api/meeting-types/<id>/scope` | POST | admin de groupe (ses groupes) / admin global | Partage : `private` ↔ `group` ↔ `global`, audité |
| `/api/meeting-types/<id>/logo` | POST, DELETE | créateur ou admin de portée | Logo PNG/JPEG ≤ 500 Ko, re-encodé Pillow (600×200, EXIF supprimé) |
| `/api/meeting-types/preview.docx` | POST | login_required | Word d'exemple de la fiche EN COURS D'ÉDITION (données factices, zéro GPU) |
| `/api/meeting-types/<id>/preview.docx` | GET | type visible/géré | Word d'exemple d'un type enregistré (avec son logo) |
| `/api/meeting-types/<id>/export` | GET | type visible/géré | Fichier d'échange `.transcria-type.json` (sans branding), audité |
| `/api/meeting-types/import` | POST | login_required | Import → type privé INACTIF « à relire » (refus explicites : enveloppe/version/branding) |
| `/api/system/status` | GET | `ACCESS_SYSTEM` | État système JSON |
| `/api/queue/status` | GET | login_required | Snapshot runtime de la file |
| `/api/queue/<id>/move-up` | POST | admin global ou admin de groupe sur périmètre | Remonte un job dans la file |
| `/api/queue/<id>/move-down` | POST | admin global ou admin de groupe sur périmètre | Descend un job dans la file |
| `/api/queue/<id>/pause` | POST | admin global ou admin de groupe sur périmètre | Met en pause une entrée de file |
| `/api/queue/<id>/resume` | POST | admin global ou admin de groupe sur périmètre | Reprend une entrée de file |
| `/api/queue/<id>/priority` | POST | admin global ou admin de groupe sur périmètre | Modifie la priorité |
| `/api/queue/<id>/cancel` | POST | admin global ou admin de groupe sur périmètre | Annule un job en file ou demande l'annulation |
| `/api/queue/e2e-test-jobs/purge` | POST | admin global | Supprime les jobs de test dont le titre commence par `E2E workflow`, hors jobs en cours |
| `/api/schedule/windows` | GET, POST | MANAGE_SCHEDULE | Liste ou crée des créneaux |
| `/api/schedule/windows/<id>` | PUT, DELETE | MANAGE_SCHEDULE | Modifie ou supprime un créneau |

**Templates** (`web/templates/`)
| Template | Description |
|---|---|
| `base.html` | Layout principal (navbar Bootstrap 5, flash messages, permissions) |
| `login.html` | Page de connexion |
| `change_password.html` | Formulaire changement de mot de passe utilisateur |
| `index.html` | Accueil : liste des traitements + bouton nouveau |
| `job_wizard.html` | Assistant 9 étapes avec formulaires interactifs (JS fetch API) |
| `meeting_types.html` | « Mes types de réunion » : galerie de cartes (bandeau réel, pastilles de palette, portée), éditeur dupliquer-d'abord avec aperçu vivant de la page de garde (mini-A4, contraste vérifié), palettes dérivées des thèmes intégrés, sections réordonnables, partage, import/export — JS `static/js/meeting_types.js` |
| `job_result.html` | Résultats & affinage : SRT (aperçu = version corrigée), qualité, exports, lien SRT Editor, **panneau du chat d'affinage** (fil de discussion, propositions applicables en un clic, versions restaurables, options de rendu, note « documents à jour ») — atteignable depuis l'étape Export du wizard et l'accueil |
| `admin_config.html` | Éditeur YAML de configuration admin |
| `users.html` | Liste des utilisateurs (admin) |
| `user_form.html` | Formulaire création/édition utilisateur |
| `groups.html` | Liste des groupes (admin global + admins de groupe) |
| `group_form.html` | Formulaire création/édition groupe + membres |
| `dashboard_status.html` | État technique (GPU, CPU, RAM, services) |
| `queue.html` | File persistante, runtime scheduler et actions admin |
| `schedule.html` | Administration des créneaux calendrier |

---

### 4.12 Queue et scheduling (`transcria/queue/`)

Le package `transcria.queue` sépare la persistance de file, le calendrier et l'allocation GPU :

| Module | Rôle |
|---|---|
| `models.py` | Modèles SQLAlchemy `JobQueueEntry` et `SchedulingWindow` |
| `store.py` | CRUD file : enqueue/dequeue, ordre, priorité, pause/reprise, aging, positions |
| `scheduler.py` | Boucle de dispatch : aging, calendrier, capacité, pré-check VRAM première phase, lancement worker |
| `calendar.py` | Évaluation des créneaux, overnight windows, priorité des actions |
| `allocator.py` | Réservations GPU thread-safe par job/phase, verrou LLM, tracking PID et `force_gpu` |
| `routes.py` | Pages `/admin/queue`, `/admin/schedule` et APIs `/api/queue/*`, `/api/schedule/*` |

Flux normal :

1. `/api/jobs/<id>/process` appelle `JobExecutorService.submit_process()`.
2. Si `workflow.queue.enabled=true`, `QueueScheduler.submit_to_queue()` crée/actualise `job_queue` et marque `extra_data.execution.status="queued"`.
3. La boucle `_dispatch_iteration()` applique l'aging, vérifie le calendrier (`pause_queue`, `limit_concurrency`, `force_gpu`), choisit les candidats éligibles et lance `_run_process()` dans un `ThreadPoolExecutor`.
4. Le pipeline réserve la VRAM au moment exact de chaque phase via `GPUAllocator`/`GPUSession`; le scheduler ne double-réserve pas.
5. En mode queue, `JobExecutorService` appelle `PipelineService.run_process(..., finalize_job_state=False)`, puis publie l'état terminal dans l'ordre `job_queue.status` → `extra_data.execution.status` → `jobs.state`.
6. En fin de pipeline, l'entrée de file passe en `done`, `failed` ou `cancelled`.
7. **VRAM insuffisante (transitoire)** : si une phase renvoie `vram_wait`, `_run_process` ne marque pas FAILED — il re-queue (`requeue_later`) + `mark_execution_waiting_vram` (statut non terminal) et alerte l'admin une fois (`alert_admin_vram_wait`). Le scheduler reprend dès que l'admission VRAM passe. Le **résumé synchrone** réutilise ce chemin via le mode de file `summary` (`JobExecutorService.SUMMARY_MODE`) : `_run_process` y exécute `run_summary` au lieu du pipeline et ne marque ni `COMPLETED` ni e-mail propriétaire (l'état est géré par `run_summary`). Voir `docs/SERVICE_RESSOURCES_GPU.md` §7.2-bis.
8. **Pipeline reprenable** : `_run_pipeline_steps` saute les phases déjà faites (`extra_data.pipeline.completed_phases` ∪ artefact, `transcria/workflow/resume.py`) → un re-queue **ne refait pas** le STT/diarisation. L'admission (`_local_required_mb` + `_done_profile_phases`) n'exige que la VRAM des **phases restantes**. État réinitialisé à la re-soumission (`api_process` → `reset_resume_state`), préservé sur re-queue auto. Cf. `docs/PIPELINE_REPRISE.md`.

Règles calendrier :

| Action | Type | Effet runtime |
|---|---|
| `pause_queue` | on/off | `_dispatch_iteration()` retourne 0 ; les jobs déjà `running` continuent |
| `limit_concurrency` | paramétrée | `get_effective_max_workers()` réduit la capacité de dispatch via `action_params.max_concurrent_jobs` |
| `force_gpu` | on/off | Autorise `GPUAllocator.force_free_gpu()` si la première phase n'a pas assez de VRAM |
| `none` | on/off | Aucun effet |

Le calendrier ne porte pas une quantité de GPUs : avec la LLM d'arbitrage multi-GPU et les phases STT/diarisation, seule la mesure runtime de `GPUAllocator` est fiable.

Les mutations sensibles de file et de calendrier appellent `audit_log()` avec les actions `job_enqueue`, `job_dequeue`, `job_prioritize`, `job_reorder`, `job_test_purge`, `queue_pause`, `queue_resume`, `schedule_window_create`, `schedule_window_modify`, `schedule_window_delete`.

### 4.13 Services (`transcria/services/`)

**`job_executor.py` — `JobExecutorService`**
| Méthode | Description |
|---|---|
| `__init__()` | Initialise le worker direct ou `QueueScheduler` selon `workflow.queue.enabled` |
| `submit_process(job_id, audio_path, mode, priority, scheduled_at, vram_profile)` | Soumet un job au scheduler persistant ou au worker direct |
| `get_runtime_snapshot()` | Retourne l'état queue/worker pour `/ready`, `/metrics`, `/api/queue/status` |
| `stop()` | Arrête le scheduler et l'executor interne (utilisé au teardown de tests et arrêt contrôlé) |
| `_run_process(job_id, audio_path, mode)` | Exécute `PipelineService.run_process(..., finalize_job_state=False)`, puis finalise `job_queue`, `execution` et `jobs.state` dans un ordre cohérent, puis appelle `_notify()` |
| `_notify(config, job, event, error)` | Helper module-level fire-and-forget : extrait email/nom de `job.owner`, appelle `send_job_notification_async()`. Absorbe toute exception — ne bloque jamais le pipeline |
| `_kill_orphaned_opencode(job_id, jobs_dir, sl)` | Tue les processus opencode orphelins via fichiers `.opencode.pid` |
| `_reconcile_interrupted_jobs(jobs_dir, sl)` | Réconcilie les jobs interrompus après redémarrage brutal |
| `init_job_executor(config, app)` | Point d'entrée d'initialisation du worker au démarrage du service |
| `shutdown_job_executor()` | Arrête proprement le worker global |

**`job_service.py` — `JobService`** (toutes méthodes statiques)
| Méthode | Description |
|---|---|
| `create(owner_id, title)` | Création d'un job |
| `upload(job_id, file)` | Upload fichier audio |
| `analyze(job_id, config)` | Analyse audio via ffprobe |
| `get_context(job_id, jobs_dir)` | Récupère le contexte complet d'un job |
| `delete(job_id)` | Suppression d'un job |

**`pipeline_service.py` — `PipelineService`**
| Méthode | Description |
|---|---|
| `run_process(job, audio_path, mode, finalize_job_state=True)` | Lance le pipeline complet de traitement pour un job déjà chargé ; en mode queue, le worker passe `False` pour publier lui-même l'état terminal |
| `_run_pipeline_steps(job, audio_path, mode, sl)` | 7 pré-traitements audio puis transcription : preflight → scène → séparation → filtrage → débruitage → normalisation → transcription, puis étapes séquentielles |
| `_define_pipeline_steps(job, audio_path, mode)` | Définit les étapes actives selon le mode (`quality` ajoute la diarisation) |
| `_config_for_mode(mode, job)` | Calcule la config effective pour le mode demandé. Si le backend est `granite` et que l'audio est `degrade` ou `audio_tres_faible`, bascule automatiquement sur le backend de production `self.config` (fallback `cohere` si nécessaire). Injecte aussi les hotwords Whisper et le biasing Cohere si activés. |
| `_release_arbitrage_llm()` | Arrête la LLM d'arbitrage en fin de pipeline (`is_arbitrage_llm_running()` → `stop_arbitrage_llm()`) |

**`config_service.py` — `ConfigService`** (toutes méthodes statiques)
| Méthode | Description |
|---|---|
| `load(path)` | Charge la config depuis un fichier YAML |
| `save(path, config)` | Sauvegarde la config sur disque |
| `get_singleton()` | Retourne le singleton config en mémoire |
| `set_singleton(config)` | Met à jour le singleton en mémoire |
| `validate(config)` | Valide la config via `validate_config()` |
| `detect_system()` | Auto-détection via `SystemDetector.detect()` |

---

### 4.13 Audit (`transcria/audit/`)

**`models.py`**

| Énumération | Valeurs |
|---|---|
| `AuditAction` | auth (`login`, `login_failed`, `logout`), jobs/file/calendrier (`job_*`, dont `job_download`, `job_external_push`, `job_test_purge`, `queue_*`, `schedule_*`), config/users/groupes/audit (`config_edit`, `user_*`, `group_*`, `audit_export`), lexiques (`lexicon_create`, `lexicon_modify`, `lexicon_delete`, `lexicon_term_add`, `lexicon_term_modify`, `lexicon_term_delete`, `lexicon_import`, `lexicon_export`, `lexicon_scope_change`, `lexicon_job_assign`) et voix (`voice_*`) |

| Classe | Colonnes |
|---|---|
| `AuditLog` | `id` (UUID PK), `timestamp` (DateTime UTC, index), `actor_id` (FK→users, nullable, index), `actor_username` (String, dénormalisé), `action` (String, index), `target_type` (String), `target_id` (String, nullable, index), `target_label` (String), `details_json` (Text, nullable), `ip_address` (String, nullable), `user_agent` (String, nullable) |

**`store.py` — `AuditStore`** (toutes méthodes statiques)

| Méthode | Description |
|---|---|
| `log(action, actor_id, actor_username, target_type, target_id, target_label, details, ip, ua)` | Écriture d'une entrée d'audit. N'interrompt jamais la requête principale (try/except). |
| `query(actor_id, action, target_type, target_id, since, until, limit, offset)` | Recherche paginée avec filtres combinables. Tri chronologique inverse. |
| `count(…)` | Compte filtré, mêmes paramètres que `query()` sans pagination. |
| `purge_expired(retention_days)` | Supprime les entrées antérieures à `now - retention_days`. Appelé automatiquement à chaque accès à la page d'accueil. |
| `purge_expired_by_policy(default_retention_days, retention_by_family)` | Applique une rétention différenciée par famille (`auth`, `job`, `lexicon`, `voice`, `config`, `other`) avec fallback global. |

**`decorator.py`**

| Fonction | Description |
|---|---|
| `audit_log(action, target_type, target_id, target_label, details)` | Capture automatiquement `current_user`, IP (`X-Forwarded-For` ou `request.remote_addr`) et User-Agent, puis appelle `AuditStore.log()`. |
| `@audit_action(action, target_type)` | Décorateur Flask : logue l'action avant/après exécution de la route. |

**`routes.py`**

| Route | Méthode | Accès |
|---|---|---|
| `/admin/audit` | GET | `ACCESS_SYSTEM` — page avec filtres (acteur, action, type cible, dates) et pagination 50/page |
| `/admin/audit/export.csv` | GET | `ACCESS_SYSTEM` — export CSV horodaté pour le DPO/référent PSSI, journalisé par `audit_export` |

Les entrées d'audit ne sont jamais supprimables par l'interface (pas de route DELETE, pas d'accès d'écriture hors `db.session` interne).

---

### 4.14 Notifications (`transcria/notifications/`)

**`mailer.py`** — envoi d'emails de notification à la fin du traitement d'un job.

**`EmailConfig`** — dataclass de configuration SMTP :

| Champ | Type | Défaut | Description |
|---|---|---|---|
| `enabled` | bool | `False` | Active/désactive les notifications |
| `smtp_host` | str | `""` | Serveur SMTP |
| `smtp_port` | int | `587` | Port SMTP |
| `smtp_username` | str | `""` | Identifiant SMTP (vide = pas d'auth) |
| `smtp_password` | str | `""` | Mot de passe SMTP |
| `use_starttls` | bool | `True` | STARTTLS (recommandé pour port 587) |
| `use_ssl` | bool | `False` | SMTPS/SSL direct (port 465) |
| `from_address` | str | `""` | Adresse expéditeur |
| `from_name` | str | `"TranscrIA"` | Nom affiché dans « De : » |
| `base_url` | str | `"http://localhost:7870"` | URL publique pour les liens emails |

| Fonction | Description |
|---|---|
| `build_email_config(cfg)` | Construit un `EmailConfig` depuis la config applicative (section `notifications.email`) |
| `send_job_notification_async(cfg, to_email, display_name, job_title, job_id, event, error)` | Point d'entrée public : vérifie la config, construit l'email HTML + texte, lance un daemon thread pour l'envoi SMTP. `event` vaut `"completed"` ou `"failed"`. Ne lève jamais. |
| `_send_smtp(ecfg, to, subject, html, text)` | Envoi SMTP effectif : STARTTLS (`use_starttls=True`), SMTPS/SSL (`use_ssl=True`) ou SMTP nu. Authentification optionnelle. |

**Modes SMTP supportés :**

| Mode | `smtp_port` | `use_starttls` | `use_ssl` |
|---|---|---|---|
| STARTTLS (recommandé) | `587` | `True` | `False` |
| SMTPS/SSL | `465` | `False` | `True` |
| SMTP nu (intranet) | `25` | `False` | `False` |

**Intégration pipeline :** `JobExecutorService._run_process()` appelle `_notify(config, job, event, error)` juste après chaque `JobStore.update_state(COMPLETED/FAILED)`. Le module extrait `job.owner.email` et `job.owner.display_name` dans le thread pipeline (app context actif), puis l'envoi SMTP se fait dans un thread daemon séparé. Aucun accès DB dans le thread SMTP.

**Prérequis :** le champ `email` du profil utilisateur doit être renseigné (interface `/admin/users/<id>/edit`). Si vide, aucune notification n'est envoyée.

---

## 5. API REST

### 5.1 Format des réponses

Succès : `{"status": "ok", ...}` ou JSON spécifique.
Erreur : `{"error": "message"}` avec code HTTP approprié (400, 403, 404).

### 5.2 Exemples

```bash
# Upload fichier
curl -X POST http://127.0.0.1:7870/api/jobs/{id}/upload -F "file=@audio.m4a"

# Analyse
curl -X POST http://127.0.0.1:7870/api/jobs/{id}/analyze

# Résumé rapide
curl -X POST http://127.0.0.1:7870/api/jobs/{id}/summary

# Sauvegarder le contexte
curl -X POST http://127.0.0.1:7870/api/jobs/{id}/context \
  -H "Content-Type: application/json" -d '{"title":"Réunion projet","language":"fr"}'

# Détection locuteurs
curl -X POST http://127.0.0.1:7870/api/jobs/{id}/speakers/detect

# Traitement complet
curl -X POST http://127.0.0.1:7870/api/jobs/{id}/process \
  -H "Content-Type: application/json" -d '{"mode":"fast"}'

# Téléchargement SRT
curl http://127.0.0.1:7870/api/jobs/{id}/download/srt -o transcription.srt
```

### 5.3 Inférence distante (frontale + nœud de ressources)

Endpoints liés à la topologie distante (cf. [`SERVICE_RESSOURCES_GPU.md`](SERVICE_RESSOURCES_GPU.md)).

**Frontale :**
- `GET /api/resources/status` — état des ressources distantes (mode de déploiement, GPU/VRAM, feu vert par moteur) pour le panneau de `dashboard_status.html` ; nœud injoignable → `reachable=false` (mode dégradé).

**Nœud de ressources** (service Flask `inference_service`, défaut `:8002`) :
- `GET /health`, `GET /ready`, `GET /models` — sondes de supervision (libres, sans clé API).
- `GET /capabilities` — inventaire : mode, GPU (index physique, VRAM libre/totale), moteurs in-process + moteurs STT déclarés avec leur santé (libre).
- `POST /engines/ensure` `{"engine": "cohere"}` — assure un moteur STT déclaré (cycle A/B/C) : `200` ready/launched, `503` busy + `Retry-After`, `404` moteur inconnu (clé API requise).
- `POST /infer/diarize`, `POST /infer/voice-embed` — `file_ref` (JSON `{"audio_path": …}`) ou `upload` multipart (clé API requise).

---

## 6. Workflow utilisateur complet (9 étapes affichées)

```
1. Login         → /login
2. Nouveau job   → /jobs/new
3. Upload        → glisser-déposer fichier audio (mp3, wav, m4a, mp4, flac, ogg)
4. Analyse       → ffprobe automatique (durée, codec, canaux, estimation temps)
5. Résumé        → Cohere ASR transcrit → pyannote détecte locuteurs → opencode résume
6. Contexte      → formulaire pré-rempli avec suggestions IA (titre, type, sujet, objectif)
7. Participants & Locuteurs → SPEAKER avec bouton écoute + champs nom/fonction/rôle pré-remplis
8. Lexique       → termes suspects détectés par l'IA, champ "Remplacer par" pré-rempli
9. Traitement    → Cohere ASR → correction opencode + LLM d'arbitrage (speakers+lexique) → qualité → export
```

### Pipeline de traitement détaillé

```
Étape 5 - Résumé :
  Phase 1 : GPUSession(<backend>-summary, VRAM backend) → GPU auto → STT rapide (chunks VAD) → offload
  Phase 1b: GPUSession(pyannote, 2 Go) → GPU auto → diarization → offload → diarization_context.md
  Phase 2 : ensure_arbitrage_llm_ready(api_model_id) → CAS A/B/C → opencode run_summary
  → meeting_context enrichi (title_suggere, type_suggere, sujet_suggere, participants_detectes, termes_suspects, summary_llm)
  → LLM reste vivante (pas de stop ici)

Étape 9 - Traitement (mode quality) :
  GPUSession(cohere-transcription, 6 Go) → GPU auto → Cohere ASR 29 chunks pyannote → offload → SRT
  → GPUSession(pyannote, 2 Go) → GPU auto → diarization supplémentaire → offload
  → ensure_arbitrage_llm_ready(api_model_id) → CAS A (LLM déjà chargée si résumé vient de tourner) → opencode correction SRT
  → qualité (16 checks, score /100) → export ZIP
  → _release_arbitrage_llm() : is_arbitrage_llm_running() → stop_arbitrage_llm() [fin pipeline]

Étape 9 - Traitement (mode fast) :
  Cohere ASR → ensure_arbitrage_llm_ready → correction opencode → qualité → export
  → _release_arbitrage_llm() [fin pipeline]
```

---

## 7. Gestion GPU

### 7.1 Modèles disponibles

| Modèle | GPU | VRAM | Lancement | Port |
|---|---|---|---|---|
| Cohere ASR (2B) | 1 GPU | ~5-6 Go | Python (transformer) | — |
| pyannote community-1 | 1 GPU | ~2 Go | Python (pyannote.audio) | — |
| LLM d'arbitrage locale | variable (selon script/config machine) | variable | `services.arbitrage_script` | `services.arbitrage_llm_port` |
| Backend LLM concurrent éventuel | variable | variable | externe au repo | `services.llm_cleanup_ports` |

Le backend d'arbitrage fourni dans ce repo est **llama.cpp** (`llama-server`) via `scripts/launch_arbitrage.sh`.
Le modèle actuellement utilisé sur cette machine est configurable : chemin modèle et paramètres du script, `workflow.*.model_id`, provider opencode et `services.arbitrage_api_model_id`.
Les références Qwen dans cette section décrivent le modèle d'exemple historique ; elles ne doivent pas être interprétées comme une dépendance applicative.

Observation machine (17 mai 2026) avec la LLM d'arbitrage active :
- `launch_arbitrage.sh` utilise `--tensor-split 1,1,1` (répartition sur 3 GPUs sur cette machine).
- `nvidia-smi` montre `llama-server` chargé sur GPU 0/1/2 avec ~18.7 GiB / ~15.6 GiB / ~15.7 GiB.
- Cette empreinte dépend du quantization, des flags (`ctx-size`, cache KV, etc.) et de la topologie GPU.

### 7.2 Cycle de vie automatique

```
Cohere ASR (résumé ou transcription) :
  GPUSession(cohere-*, 6 Go)
    → ensure_free() → scan des GPUs visibles → GPU avec le plus de VRAM libre
    → charge modèle → transcrit → offload auto à la sortie du context manager

pyannote (diarization ou speaker_detection) :
  GPUSession(pyannote, 2 Go)
    → ensure_free() → GPU auto (évite les GPUs occupés par la LLM d'arbitrage)
    → diarize() → diarizer.offload() → offload_all() à la sortie

LLM arbitrage (résumé puis correction) :
  ensure_arbitrage_llm_ready(api_model_id)
    → CAS A : LLM déjà saine → opencode démarre immédiatement
    → CAS C : lancement llama-server → attente port → opencode
  [LLM reste vivante entre résumé et correction — CAS A garanti si le serveur reste sain]

Fin de pipeline :
  PipelineService._release_arbitrage_llm()
    → is_arbitrage_llm_running() → si True : stop_arbitrage_llm()
  [unique point d'arrêt de la LLM, dans le finally de _execute_pipeline]
```

### 7.3 Monitoring

Le dashboard-llm (port 5001) fournit l'état GPU en temps réel via `/api/v1/gpus` et `/api/v1/gpus/processes`. Le `VRAMManager` utilise ce dashboard en priorité, avec fallback sur PyTorch `torch.cuda.mem_get_info()`. Si `CUDA_VISIBLE_DEVICES` est défini, les ids du dashboard sont traités comme physiques puis remappés avant construction du device `cuda:N`.

`is_port_open()` effectue un test d'inférence réel (`/v1/models` puis `/v1/completions`) pour vérifier que le modèle répond réellement, pas uniquement que le port est ouvert.

---

## 8. opencode

Le fichier `~/.config/opencode/opencode.json` doit au minimum définir le provider `local` utilisé par `model_id` (par défaut `local/arbitrage` — alias **générique stable**, cf. AGENTS.md).

`OpenCodeRunner` appelle :
```
opencode run --format json --model local/arbitrage <instruction> -f <prompt_file>
```

Le résultat est parsé comme NDJSON (un objet JSON par ligne). Les événements de type `text` fournissent le texte généré, les événements `tool_use` les appels d'outils.

---

## 9. Base de données

TranscrIA utilise Flask-SQLAlchemy (`transcria/database.py`). **PostgreSQL** (via `psycopg`) est la cible de production depuis la **Phase A** ; SQLite reste un repli mono-process pour le dev/les tests. Le DSN vient de `TRANSCRIA_DATABASE_URL` (prioritaire) puis `storage.database_url`. Le schéma est géré par **Alembic** : `start.sh` lance `alembic upgrade head` au démarrage, et le workflow d'évolution est `éditer le modèle → alembic revision --autogenerate → relire → upgrade`, gardé par le test anti-dérive `tests/test_alembic_migrations.py`. `db.create_all()` ne sert plus qu'au bootstrap dev/tests. Migration des données SQLite→PostgreSQL : `scripts/migrate_sqlite_to_postgres.py`. La concurrence applicative (Phase B : claim `FOR UPDATE SKIP LOCKED`, verrou consultatif d'ordonnanceur unique, `LISTEN/NOTIFY`) repose sur PostgreSQL — voir `docs/CONCURRENCE_ET_CHARGE_PHASE_B.md`.

### 9.1 Tables

**users**
| Colonne | Type | Description |
|---|---|---|
| id | VARCHAR(36) PK | UUID |
| username | VARCHAR(80) UNIQUE | Identifiant |
| display_name | VARCHAR(160) | Nom affiché |
| email | VARCHAR(255) | Email |
| password_hash | VARCHAR(255) | Hash werkzeug |
| role | VARCHAR(20) | admin/manager/operator/viewer |
| is_active | BOOLEAN | Compte actif |
| created_at | DATETIME | Date création |
| last_login | DATETIME | Dernière connexion |

**jobs**
| Colonne | Type | Description |
|---|---|---|
| id | VARCHAR(36) PK | UUID |
| owner_id | VARCHAR(36) FK→users | Propriétaire |
| title | VARCHAR(255) | Titre |
| state | VARCHAR(40) | État workflow |
| processing_mode | VARCHAR(20) | fast/quality |
| extra_data_json | TEXT | Métadonnées JSON |
| error_message | TEXT | Message d'erreur |
| created_at | DATETIME | Date création |
| updated_at | DATETIME | Dernière modification |

**audit_logs**

| Colonne | Type | Description |
|---|---|---|
| id | VARCHAR(36) PK | UUID |
| timestamp | DATETIME INDEX | Horodatage UTC |
| actor_id | VARCHAR(36) FK→users | Auteur de l'action (nullable = système) |
| actor_username | VARCHAR(80) | Login dénormalisé (survit à la suppression du compte) |
| action | VARCHAR(40) INDEX | Type d'action (enum AuditAction) |
| target_type | VARCHAR(20) | Catégorie cible (job, user, group, config, lexicon, voice, system) |
| target_id | VARCHAR(36) INDEX | UUID de la ressource cible |
| target_label | VARCHAR(255) | Libellé lisible de la cible |
| details_json | TEXT | Détails structurés (JSON, sans PII en clair) |
| ip_address | VARCHAR(45) | IP du poste client |
| user_agent | VARCHAR(512) | Navigateur/client HTTP |

---

## 10. Sécurité

- Authentification obligatoire sur toutes les routes (sauf `/login`)
- Mots de passe hashés (werkzeug `generate_password_hash`/`check_password_hash`)
- Autorisation par rôle : décorateur `@requires(Permission.XXX)`
- Contrôle d'accès aux jobs : `_require_job_access()` vérifie `owner_id`
- Uploads limités aux extensions configurées
- Rétention configurable (`security.retention_days`)
- Taille max d'upload : 1 Go (`MAX_CONTENT_LENGTH`)
- Clé secrète Flask : `TRANSCRIA_SECRET` env var ou `os.urandom(32).hex()`

**Audit de sécurité** : toutes les actions sensibles sont journalisées dans la table `audit_logs` (cf. §4.13). Les entrées sont conservées `security.audit_retention_days` jours (défaut 1095) avec surcharge possible par `security.audit_retention_by_family`, ne sont pas supprimables par l'interface, et sont exportables en CSV depuis `/admin/audit`. Les actions lexiques journalisent uniquement des métadonnées et des signaux de sensibilité, jamais les termes en clair.

**Vulnérabilités connues** : les sujets actifs sont suivis dans la documentation courante et les tests de non-régression du dépôt.

---

## 11. Tests

La suite pytest couvre tous les modules. Lancer avec :
```bash
cd transcria && python -m pytest tests/ -v
```

2500+ tests au total (sans les E2E GPU). Organisation :

| Fichier | Tests | Couverture |
|---|---|---|
| `test_audio.py` | 64 | Analyse de scène worker, AudioSceneAnalyzer, séparation sources, genre |
| `test_audit.py` | 12 | AuditStore, rétention par famille, export CSV |
| `test_auth.py` | 17 | Rôles, modèles, permissions, décorateur |
| `test_auth_store.py` | 14 | CRUD utilisateurs, groupes |
| `test_bench_tools.py` | 13 | Outils benchmark audio |
| `test_central_lexicon.py` | 28 | CentralLexiconStore, CentralLexiconService, routes admin |
| `test_config.py` | 40 | Chargement YAML, sauvegarde config, env var, debug |
| `test_context.py` | 27 | Meeting, participants, lexique, builder |
| `test_diarization.py` | 37 | DiarizerService, SortformerDiarizer, BaseDiarizer, diarizer_factory |
| `test_doctor.py` | 38 | Préflight `transcria doctor` : diff de schéma, script/serveur LLM, opencode, nœuds, dossiers, exit code, smoke opencode→LLM (`--llm-smoke`, dont pré-sonde « LLM down → FAIL rapide ») |
| `test_incident_e62295c1.py` | 10 | Suites incident : détection « 0 texte » LLM (mtime), retry ≤3 + `summary_llm_failed` relançable, saut STT en cache, arrêt LLM inactive pour débloquer un STT, saut réservation locale en STT distant |
| `test_edge_cases.py` | 17 | Cas limites contexte/exports/transitions |
| `test_exports.py` | 3 | PackageBuilder |
| `test_gpu.py` | 72 | VRAMManager, `CUDA_VISIBLE_DEVICES`, libération VRAM ciblée, diagnostic lancement LLM |
| `test_gpu_allocator.py` | 7 | Réservations GPU, remapping CUDA visible, verrou LLM |
| `test_integrations.py` | 12 | Dashboard, SRT Editor, OpenCodeRunner |
| `test_job_service.py` | 2 | JobService |
| `test_job_store.py` | 15 | JobStore CRUD, purge rétention |
| `test_jobs.py` | 19 | Job model, filesystem |
| `test_mailer.py` | 20 | EmailConfig, templates HTML/texte, dispatch async, modes SMTP, XSS |
| `test_opencode_runner.py` | 54 | opencode, parsing résumé, correction |
| `test_pipeline_service.py` | 19 | Analyse de scène, filtrage, normalisation, séparation, ordre pipeline |
| `test_quality.py` | 19 | SRT checks, lexique |
| `test_quality_deep.py` | 37 | SRT réel, rapport intégré, checks approfondis |
| `test_queue_calendar.py` | 10 | SchedulingCalendar, règles calendrier, overnight windows |
| `test_queue_scheduler.py` | 6 | QueueScheduler dispatch, aging |
| `test_queue_store.py` | 6 | QueueStore CRUD, priorités, pause/reprise |
| `test_stt.py` | 76 | STT, timestamps, alignement, speaker clips, fiabilité |
| `test_summary_generator.py` | 1 | Génération résumé rapide |
| `test_voice.py` | 13 | VoiceStore, empreintes, matching, consentements |
| `test_voice_e2e.py` | 1 | Flux E2E voix enregistrées |
| `test_web_api.py` | 54 | Routes web, login, jobs, upload, admin config, lexique debug |
| `test_web_edge_cases.py` | 53 | Erreurs API, rôles, accès jobs, pipeline |
| `test_web_helpers.py` | 13 | Helpers web (audio diagnostic, enrichissement lexique, locuteurs) |
| `test_workflow.py` | 30 | États, transitions, runner |
| `test_workflow_runner.py` | 64 | Runner, correction, résumé, genre locuteur |
| `conftest.py` | — | Fixtures pytest (app, client, admin/operator/viewer) |

---

## 12. Structure disque runtime

```
instance/
  transcrIA.db                    # Base SQLite (repli dev ; prod = PostgreSQL)

voices/
  subjects/{uuid}/consents/       # Preuves de consentement vocal
  subjects/{uuid}/references/     # Audios de référence temporaires

jobs/{uuid}/
  input/
    original.{ext}               # Fichier audio original
    vocals.wav                   # Piste vocale extraite (Demucs, si séparation appliquée)
    scene_filtered.wav           # Audio filtré pré-STT (si audio_scene_filter activé)
    denoised.wav                 # Audio débruité pré-STT (si audio_denoise activé)
    normalized.wav               # Audio normalisé pré-STT (si audio_normalization activé)
  metadata/
    audio_analysis.json          # Résultat ffprobe
    audio_preflight.json         # Pré-diagnostic acoustique (RMS, SNR, bande passante, clipping, flags)
    audio_quality_decision.json  # Décision qualité (level, score, scene_findings, scene_metrics)
    audio_scene.json             # Analyse de scène (ratios, segments, genre vocal) si activé
    audio_scene_filter.json      # Filtrage pré-STT appliqué (preserve_timeline=true) si activé
    audio_denoise.json           # Débruitage pré-STT appliqué (preserve_timeline=true) si activé
    audio_normalization.json     # Normalisation pré-STT appliquée (preserve_timeline=true) si activé
    audio_excerpts/*.wav         # Extraits temporisés pour validation audio du lexique
    transcription.srt            # SRT brut (Cohere, Whisper ou Granite + speakers appliqués)
    transcription_corrigee.srt   # SRT corrigé (opencode)
    transcription_segments.json  # Segments détaillés
    transcription_metadata.json  # Métadonnées post-transcription (backend, chunking_mode, chunking_forced_30s_reason si audio_tres_faible, vad_final_enabled)
    speakers_map.json            # Mapping speakers
    correction_report.md         # Rapport de correction
  summary/
    quick_transcript.txt          # Transcription brute
    summary.json                  # Métadonnées résumé
    diarization_context.md         # Contexte acoustique pyannote pour le résumé LLM
    summary.md                    # Résumé markdown
  context/
    meeting_context.json          # Contexte réunion
    participants.json             # Liste participants
    session_lexicon.json          # Lexique de session
    session_lexicon.txt           # Lexique en texte
    job_context.yaml              # Contexte agrégé (YAML)
    job_context.json              # Contexte agrégé (JSON)
  speakers/
    speaker_turns.json            # Turns pyannote (exclusive_turns inclus)
    speaker_stats.json            # Stats par locuteur (gender inclus si attribution acoustique)
    diarization_audio.json         # Métadonnées du cache PCM pyannote (si prepare_pcm_audio)
    diarization_16k_mono.wav       # WAV PCM 16 kHz mono réservé à pyannote (si activé)
    diarization_checkpoint.json   # Empreinte audio+modèle pour réutilisation pyannote
    speaker_embeddings.json       # Checkpoint acoustique par locuteur
    speaker_mapping.json          # Mapping SPEAKER_XX → participant
    voice_matches.json            # Suggestions voix enregistrées, sans embeddings
    speaker_clips.json            # Liste des extraits audio
    samples/                      # Fichiers WAV extraits
  quality/
    quality_report.json           # Rapport qualité structuré
    quality_report.md             # Rapport qualité markdown
    review_points.json            # Points à vérifier
  exports/
    transcrIA_job_{uuid}.zip       # Package final
```

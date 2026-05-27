# TranscrIA — Documentation technique

## 1. Vue d'ensemble

TranscrIA est un portail guidé de transcription de réunion destiné aux utilisateurs non techniciens (secrétaires de réunion). Il orchestre le dépôt d'un fichier audio/vidéo jusqu'à la production d'un package exploitable contenant le SRT corrigé (speakers + lexique), le contexte, les participants, le lexique, le rapport qualité, le rapport de correction et les points à vérifier.

**Stack :** Python 3.11+ / Flask / SQLAlchemy (SQLite) / Jinja2 / Cohere ASR / faster-whisper large-v3 / Granite Speech expérimental / Parakeet TDT 0.6B v3 expérimental (NeMo) / pyannote / torchaudio CTC / opencode (LLM locale d'arbitrage) / Bootstrap 5

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
- `GET /health` retourne un statut JSON simple du service et de la base SQLite
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
│   │   ├── models.py              # Modèle Job, JobState (20 états)
│   │   ├── store.py               # JobStore (CRUD jobs, count_jobs)
│   │   └── filesystem.py          # JobFilesystem (I/O disque, save_json/load_json/save_text/load_text/save_upload)
│   │
│   ├── workflow/                  # Moteur de workflow 9 étapes affichées
│   │   ├── __init__.py
│   │   ├── states.py              # WorkflowState (compute_statuses, get_next_step), StepStatus
│   │   ├── steps.py               # WORKFLOW_STEPS + helpers de navigation
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
│   ├── exports/                   # Package ZIP final
│   │   ├── __init__.py
│   │   └── package_builder.py    # PackageBuilder (ZIP avec tous les fichiers)
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
│   │   └── _port_utils.py         # is_port_open() partagé entre vram_manager et llm_backend
│   │
│   ├── services/                  # Services métier
│   │   ├── job_executor.py       # JobExecutorService (worker thread)
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
│       ├── summary_prompt.txt      # Prompt résumé structuré (opencode) — v2.0 (394 lignes)
│       ├── correction_prompt.txt   # Prompt correction SRT (speakers + lexique + orthographe) — v1.9 (612 lignes)
│
├── tests/                         # suite pytest + E2E
│   ├── conftest.py                # Fixtures (app, client, admin/operator/viewer)
│   ├── test_auth.py               # 17 tests — Rôles, modèles, permissions
│   ├── test_auth_store.py         # 14 tests — CRUD utilisateurs, groupes
│   ├── test_config.py             # 24 tests — Chargement YAML, sauvegarde config, debug
│   ├── test_context.py            # 19 tests — Meeting, participants, lexique, builder
│   ├── test_diarization.py        # 12 tests — Diarisation, checkpoints, clips
│   ├── test_edge_cases.py         # 17 tests — Cas limites contexte/exports/transitions
│   ├── test_exports.py            # 3 tests — PackageBuilder
│   ├── test_gpu.py                # 59 tests — VRAMManager
│   ├── test_integrations.py       # 12 tests — DashboardClient, SrtEditorLink, OpenCodeRunner
│   ├── test_jobs.py               # 19 tests — Job model, filesystem
│   ├── test_job_store.py          # 15 tests — JobStore CRUD, purge rétention
│   ├── test_opencode_runner.py    # 44 tests — opencode, parsing résumé, correction
│   ├── test_audio.py              # 45 tests — Analyse de scène worker, AudioSceneAnalyzer, séparation sources
│   ├── test_pipeline_service.py   # 13 tests — Analyse de scène, séparation, filtrage, normalisation, ordre pipeline
│   ├── test_quality.py            # 19 tests — SRTChecker, LexiconChecker
│   ├── test_quality_deep.py       # 19 tests — Tests approfondis qualité avec SRT réel
│   ├── test_stt.py                # 32 tests — STT, timestamps, alignement, speaker clips
│   ├── test_summary_generator.py  # 1 test — Résumé rapide
│   ├── test_web_api.py            # 38 tests — Routes web (login, jobs, upload, admin config)
│   ├── test_web_edge_cases.py     # 50 tests — Erreurs API, rôles, accès jobs, pipeline
│   ├── test_workflow.py           # 30 tests — États, transitions, runner
│   └── test_workflow_runner.py    # 55 tests — Runner, correction, résumé, genre locuteur
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
  database_url: "sqlite:///transcrIA.db"

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
    model_id: "qwen3-35b-arbitrage-ud-q8_k_xl"
    api_base: "http://127.0.0.1:8080/v1"
    timeout_seconds: 1800
    use_chat_api: true
  arbitration_llm:
    enabled: false
    model_id: "local/qwen3-35b-arbitrage"
    api_base: "http://127.0.0.1:8080/v1"
    timeout_seconds: 7200
    opencode_bin: "opencode"

security:
  retention_days: 365
  allow_job_delete: true
  allowed_upload_extensions: [".mp3", ".wav", ".m4a", ".mp4", ".flac", ".ogg"]
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
- **Fusion de micro-segments** : les segments courts (< seuil configurable, même locuteur, gap court) sont fusionnés avec le segment précédent pour réduire les artefacts de fragmentation. Configurable via `merge_short_segments` (défaut `true`), `short_segment_max_s` (défaut 0.45), `short_segment_max_words` (défaut 2), `merge_gap_s` (défaut 0.5), `merge_max_chars` (défaut 220).

Les deux opérations sont tracées dans les logs du pipeline (`removed_artifacts=N, merged_short_segments=M`).

Le VAD Silero reste actif par défaut en résumé. `AdaptiveVADConfig` adapte les seuils à partir de `metadata/audio_quality_decision.json` sans modifier la configuration globale. La transcription finale a le VAD désactivé par défaut (`enabled_final=false`) et l'auto-activation sur audio dégradé également (`auto_enable_final_on_degraded=false`). Le VAD interne de Whisper (`vad_filter`) est désactivé par défaut. Voir `docs/VAD_OR_NOT.md` pour l'analyse complète et les recommandations par type de fichier.

Pyannote écrit maintenant `speakers/diarization_checkpoint.json` pour réutiliser les tours si l'audio et le modèle n'ont pas changé, et `speakers/speaker_embeddings.json` comme checkpoint acoustique par locuteur.

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
| `JobFilesystem(jobs_dir, job_id)` | `save_json()`, `load_json()`, `save_text()`, `load_text()`, `save_upload()`, `get_original_audio_path()`, `cleanup()` |

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

**`runner.py` — `WorkflowRunner`**
| Méthode | Description | GPU |
|---|---|---|
| `run_analyze(job, audio_path)` | ffprobe | — |
| `run_summary(job, audio_path, config)` | Cohere transcription → pyannote si activé → opencode résumé | GPUSession auto |
| `run_speaker_detection(job, audio_path, config)` | pyannote diarization + formatage via GPUSession | GPUSession auto |
| `run_transcription(job, audio_path, config)` | Cohere ASR → segments → apply_speakers → SRT | GPUSession auto |
| `run_diarization(job, audio_path, config)` | pyannote speaker mapping via GPUSession | GPUSession auto |
| `run_correction(job, config)` | opencode + LLM d'arbitrage : correction speakers+lexique+orthographe | LLM arbitrage |
| `run_quality_checks(job, config)` | 16 contrôles qualité | — |
| `build_export(job, config)` | Package ZIP | — |

Pipeline de traitement complet (`api_process`) :
```
run_transcription → cleanup_transcription → run_diarization (si quality) → run_correction → run_quality_checks → build_export
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
  (CAS A garanti si run_summary vient de tourner — LLM déjà chargée, même PID)
```

L'arrêt de la LLM est délégué à `PipelineService._release_arbitrage_llm()` via `finally` en fin de pipeline.

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
| `diarize(job, audio_path)` | Charge pipeline pyannote → inférence → turns + extraction `exclusive_speaker_diarization` dans `exclusive_turns` (fallback `AttributeError` → turns standard) → stats → sauvegarde |

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
| `save(job, jobs_dir, context_data)` | Merge avec existing en préservant les champs LLM (summary_llm, title_suggere, etc.) |
| `auto_suggest(job, jobs_dir)` | Suggestions basées sur le résumé |
| `default_context()` | Valeurs par défaut (language="fr", meeting_type="Réunion interne", sensitivity="normal") |

`MEETING_TYPES` : Réunion interne, Réunion projet, Réunion technique, Formation, Réunion médicale / santé, RH, Entretien, Autre

**`participants.py` — `ParticipantsManager`**
| Méthode | Description |
|---|---|
| `get(job, jobs_dir)` | Charge la liste ou retourne [] |
| `save(job, jobs_dir, participants)` | Valide (strip, id auto, default) et sauvegarde |
| `default_participant()` | Retourne {id:"", name:"", function:"", service:"", role:"", is_animator:False, expected:True, comment:""} |

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
| `central_lexicon_store.py` | CRUD, import CSV/TXT, permissions admin/admin groupe, périmètre job→groupes, stats d'usage et contrôles qualité |
| `central_lexicon_service.py` | Préfiltrage affichage avec raison de proposition, fusion central + LLM + session et filtrage par présence dans le SRT |
| `central_lexicon_routes.py` | Interface `/admin/lexicons`, stats et alertes qualité |

Le pré-remplissage de l'étape 6 utilise les lexiques globaux et les lexiques des groupes du propriétaire du job. `context/selected_lexicons.json` mémorise les lexiques cochés pour le job ; absent, tous les lexiques accessibles sont sélectionnés. `prefilter_lexicon_entries_for_display()` masque avant affichage les entrées centrales normales sans occurrence dans le transcript/résumé, tout en conservant les priorités `critique`/`importante`. Il ajoute `_display_reason` (`term_presence`, `variant_presence`, `priority`) pour expliquer dans l'UI pourquoi un terme est proposé. Une session déjà sauvegardée reste prioritaire et n'est pas écrasée. Avant correction, `WorkflowRunner.run_correction()` écrit `context/session_lexicon_filtered.json` : termes présents dans le SRT par forme ou variante, plus entrées `critique`/`importante` conservées en préservation.

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
7ter. Garde-fous déterministes : noms de locuteurs modifiés (`speaker_name_violations`), segments marqués étrangers (`foreign_segments`), segments non latins (`non_latin_segments`), segments courts suspects de bruit ASR (`suspicious_short_segments`) — ces quatre sous-checks alimentent le `review_load`
8bis. Flags du pré-diagnostic acoustique (`audio_preflight_flags`) — `audio_faible`, `audio_tres_faible`, `snr_faible`, etc.
9. Segments suspects : `no_speech_prob` élevé (hallucination Whisper sur silence/audio dégradé)
10. Segments suspects : faible confiance mots (`suspect_low_word_confidence`) — ratio de mots à faible probabilité > seuil
11. Fiabilité segmentaire post-STT (`segment_reliability`) — classification ok/suspect/degrade par segment via `SegmentReliabilityScorer`
12. Couverture audio (< 80%)
13. Ratio mots/seconde suspect (< 0.5 ou > 10)

Score = max(0, 100 - warnings × 5). Sauvegarde quality_report.json, quality_report.md, review_points.json.

**`review_points.py` — `ReviewPoints`**
| Méthode | Description |
|---|---|
| `generate(quality_report)` | Traduit les checks en phrases utilisateur |

---

### 4.8 Exports (`transcria/exports/`)

**`package_builder.py` — `PackageBuilder`**
| Méthode | Description |
|---|---|
| `build_package(job)` | Crée `transcrIA_job_{uuid}.zip` avec tous les fichiers |

Contenu du ZIP :
```
audio/original.{ext}
subtitles/transcription.srt              # SRT corrigé si disponible, sinon original
subtitles/transcription_segments.json     # si disponible
context/job_context.yaml, meeting_context.json, participants.json, session_lexicon.json
context/speaker_mapping.json, speaker_stats.json
quality/quality_report.md, quality_report.json, review_points.json
quality/correction_report.md             # si disponible
```

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
- `services.arbitrage_llm_port` (8080), `services.llm_cleanup_ports` (`[8000]`)
- `gpu.cohere_vram_mb`, `gpu.pyannote_vram_mb`, `gpu.llm_vram_mb`, `gpu.min_free_vram_mb`

| Méthode | Description |
|---|---|
| `get_gpu_info()` | Dashboard API ou fallback PyTorch |
| `get_free_vram_mb(gpu_index)` | VRAM libre en Mo |
| `get_best_gpu(required_mb)` | Meilleur GPU disponible (≥ required + MIN_FREE) |
| `ensure_free(required_mb, preferred_gpu)` | Scanne tous les GPUs si le GPU courant est insuffisant → sélectionne le meilleur → log scan complet |
| `is_arbitrage_llm_running()` | Retourne True si un processus écoute sur `arbitrage_llm_port` (lsof) — utilisé par `_release_arbitrage_llm` avant d'appeler stop |
| `ensure_arbitrage_llm_ready(expected_model_id)` | Point d'entrée unique avant usage LLM : CAS A réutilisation, CAS B mauvais modèle, CAS C lancement — chaque chemin logué explicitement |
| `launch_arbitrage_llm()` | Lance `services.arbitrage_script` → attend port (timeout 600s) |
| `stop_arbitrage_llm()` | Arrête la LLM d'arbitrage via `services.stop_script`, puis libère `arbitrage_llm_port` en fallback |
| `stop_cleanup_llm_ports()` | Libère les ports `services.llm_cleanup_ports` (vLLM, SGLang, llama.cpp, ik_llama.cpp ou autre backend concurrent) |
| `free_all_gpus()` | stop_cleanup_llm_ports + stop_arbitrage_llm + offload_all (reset forcé uniquement) |
| `is_port_open(port)` | Vérifie `/v1/models` accessible + teste une inférence réelle `/v1/completions` |
| `_log_all_gpus(label)` | Logue VRAM libre/totale/utilisée de chaque GPU (utilisé par ensure_free lors d'un basculement) |
| `_wait_for_port(port, timeout)` | Boucle d'attente avec `is_port_open` toutes les 5s |
| `track_model / untrack_model` | Enregistre/désenregistre un modèle chargé |
| `offload_all()` | Vide `_loaded_models` + gc + cuda.empty_cache |

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
| `run_summary(transcript_path, context_path, diarization_context_path)` | Génère un résumé structuré via opencode + LLM d'arbitrage. Inclut la diarization acoustique si disponible, lit le fichier summary.md produit et le parse |
| `run_correction(srt_path, context_path, lexicon_path)` | Correction SRT : lit transcription.srt + job_context.yaml + lexique filtré, écrit transcription_corrigee.srt + correction_report.md |
| `_parse_structured_summary(text)` | Parse le markdown LLM en dictionnaire avec regex (title_suggere, type_suggere, sujet_suggere, objectif_suggere, notes_suggeres, participants_detectes, mots_cles, speaker_count, termes_suspects) |

**Fichiers prompts** dans `configs/prompts/` :
- `summary_prompt.txt` : Prompt système pour le résumé structuré
- `correction_prompt.txt` : Prompt pour la correction SRT (speakers + lexique + orthographe)

---

### 4.11 Web (`transcria/web/`)

**`routes.py` — Routes**

Le fichier contient les routes pages + API. Les routes liées aux jobs passent par `_require_job_access()` ou `_get_job_for_api()` pour vérifier que l'utilisateur est propriétaire du job ou admin.

| Route | Méthode | Auth | Description |
|---|---|---|---|
| `/health` | GET | Publique | Statut service + base SQLite |
| `/ready` | GET | Publique | Préparation du worker interne |
| `/metrics` | GET | Publique | Métriques Prometheus (`transcria_up`, `transcria_jobs_total`, `transcria_jobs_state`) |
| `/` | GET | login_required | Accueil (liste des traitements) |
| `/jobs/new` | POST | login_required + CREATE_JOBS | Création traitement |
| `/jobs/<id>` | GET | login_required + owner check | Assistant wizard 9 étapes |
| `/jobs/<id>/result` | GET | login_required + owner check | Page résultat |
| `/jobs/<id>/delete` | POST | login_required + DELETE_JOBS | Suppression traitement |
| `/system` | GET | login_required + ACCESS_SYSTEM | État technique (GPU dashboard) |
| `/admin/config` | GET, POST | login_required + MANAGE_CONFIG | Édition YAML de la configuration |
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
| `/api/jobs/<id>/audio/excerpt` | GET | login_required + owner check | Extrait WAV temporisé pour valider un contexte de lexique |
| `/api/jobs/<id>/speakers/detect` | POST | login_required + owner/admin check | Détection locuteurs |
| `/api/jobs/<id>/speakers/voice-match` | POST | login_required + owner/admin check | Suggestions depuis les voix enregistrées accessibles au job |
| `/api/jobs/<id>/speakers/map` | POST | login_required + owner/admin check | Mapping SPEAKER_XX |
| `/api/jobs/<id>/speakers/clips` | GET | login_required + owner check | Liste extraits audio |
| `/api/jobs/<id>/speakers/clip/<name>` | GET | login_required + owner check | Fichier WAV d'un extrait |
| `/api/jobs/<id>/process` | POST | login_required + owner/admin check | Traitement complet |
| `/api/jobs/<id>/quality` | POST | login_required + owner/admin check | Rapport qualité |
| `/api/jobs/<id>/export` | POST | login_required + owner/admin check | Construction package |
| `/api/jobs/<id>/download/srt` | GET | login_required + owner check | Téléchargement SRT |
| `/api/jobs/<id>/download/package` | GET | login_required + owner check | Téléchargement ZIP |
| `/api/jobs/<id>/download/audio` | GET | login_required + owner check | Téléchargement audio |
| `/api/jobs/<id>/push-to-editor` | POST | login_required + owner/admin check | Envoi vers SRT Editor EASY |
| `/api/jobs/<id>/status` | GET | login_required + owner check | Statut job JSON (polling) |
| `/api/jobs/<id>/reprocess` | POST | login_required + owner/admin check | Relance le traitement |
| `/api/jobs/<id>/status` | GET | login_required + owner check | Statut job JSON |
| `/api/jobs/<id>/reprocess` | POST | login_required + owner/admin check | Relance le traitement |
| `/api/system/status` | GET | `ACCESS_SYSTEM` | État système JSON |

**Templates** (`web/templates/`)
| Template | Description |
|---|---|
| `base.html` | Layout principal (navbar Bootstrap 5, flash messages, permissions) |
| `login.html` | Page de connexion |
| `change_password.html` | Formulaire changement de mot de passe utilisateur |
| `index.html` | Accueil : liste des traitements + bouton nouveau |
| `job_wizard.html` | Assistant 9 étapes avec formulaires interactifs (JS fetch API) |
| `job_result.html` | Résultat : SRT, qualité, exports, lien SRT Editor |
| `admin_config.html` | Éditeur YAML de configuration admin |
| `users.html` | Liste des utilisateurs (admin) |
| `user_form.html` | Formulaire création/édition utilisateur |
| `groups.html` | Liste des groupes (admin global + admins de groupe) |
| `group_form.html` | Formulaire création/édition groupe + membres |
| `dashboard_status.html` | État technique (GPU, CPU, RAM, services) |

---

### 4.12 Services (`transcria/services/`)

**`job_executor.py` — `JobExecutorService`**
| Méthode | Description |
|---|---|
| `__init__()` | Initialise le `ThreadPoolExecutor` (max_concurrent_jobs depuis config) |
| `_kill_orphaned_opencode(job_id, jobs_dir, sl)` | Tue les processus opencode orphelins via fichiers `.opencode.pid` |
| `_reconcile_interrupted_jobs(jobs_dir, sl)` | Réconcilie les jobs interrompus après redémarrage brutal |
| `init_job_executor(config, app)` | Point d'entrée d'initialisation du worker au démarrage du service |

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
| `run_process(job, audio_path, mode)` | Lance le pipeline complet de traitement pour un job déjà chargé |
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
    → ensure_free() → scan tous GPUs → GPU avec le plus de VRAM libre
    → charge modèle → transcrit → offload auto à la sortie du context manager

pyannote (diarization ou speaker_detection) :
  GPUSession(pyannote, 2 Go)
    → ensure_free() → GPU auto (évite les GPUs occupés par la LLM d'arbitrage)
    → diarize() → diarizer.offload() → offload_all() à la sortie

LLM arbitrage (résumé puis correction) :
  ensure_arbitrage_llm_ready(api_model_id)
    → CAS A : LLM déjà saine → opencode démarre immédiatement
    → CAS C : lancement llama-server → attente port → opencode
  [LLM reste vivante entre résumé et correction — même PID, CAS A garanti]

Fin de pipeline :
  PipelineService._release_arbitrage_llm()
    → is_arbitrage_llm_running() → si True : stop_arbitrage_llm()
  [unique point d'arrêt de la LLM, dans le finally de _execute_pipeline]
```

### 7.3 Monitoring

Le dashboard-llm (port 5001) fournit l'état GPU en temps réel via `/api/v1/gpus` et `/api/v1/gpus/processes`. Le `VRAMManager` utilise ce dashboard en priorité, avec fallback sur PyTorch `torch.cuda.mem_get_info()`.

`is_port_open()` effectue un test d'inférence réel (`/v1/models` puis `/v1/completions`) pour vérifier que le modèle répond réellement, pas uniquement que le port est ouvert.

---

## 8. opencode

Le fichier `~/.config/opencode/opencode.json` doit au minimum définir le provider `local` utilisé par `model_id` (par défaut `local/qwen3-35b-arbitrage`).

`OpenCodeRunner` appelle :
```
opencode run --format json --model local/qwen3-35b-arbitrage <instruction> -f <prompt_file>
```

Le résultat est parsé comme NDJSON (un objet JSON par ligne). Les événements de type `text` fournissent le texte généré, les événements `tool_use` les appels d'outils.

---

## 9. Base de données

TranscrIA utilise Flask-SQLAlchemy (`transcria/database.py`) avec SQLite par défaut. Au démarrage, `db.create_all()` crée les tables absentes. Cela suffit pour ajouter de nouvelles tables comme `groups`, mais ne migre pas les colonnes existantes. Toute évolution destructive ou ajout de colonne sur table existante doit passer par Flask-Migrate ou une migration manuelle documentée et testée.

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

**Vulnérabilités connues** : les sujets actifs sont suivis dans la documentation courante et les tests de non-régression du dépôt.

---

## 11. Tests

La suite pytest couvre tous les modules. Lancer avec :
```bash
cd transcria && python -m pytest tests/ -v
```

Organisation :
| Fichier | Tests | Couverture |
|---|---|---|
| `test_audio.py` | 45 | Analyse de scène worker, AudioSceneAnalyzer, séparation sources, genre |
| `test_auth.py` | 17 | Rôles, modèles, permissions, décorateur |
| `test_auth_store.py` | 14 | CRUD utilisateurs, groupes |
| `test_config.py` | 24 | Chargement YAML, sauvegarde config, env var, debug |
| `test_context.py` | 19 | Meeting, participants, lexique, builder |
| `test_diarization.py` | 37 | DiarizerService, SortformerDiarizer, BaseDiarizer, diarizer_factory (checkpoints, clips, parsing, normalisation, factory pattern) |
| `test_edge_cases.py` | 17 | Cas limites, transitions workflow |
| `test_exports.py` | 3 | PackageBuilder |
| `test_gpu.py` | 59 | VRAMManager |
| `test_integrations.py` | 12 | Dashboard, SRT Editor, OpenCodeRunner |
| `test_jobs.py` | 19 | Job model, filesystem |
| `test_job_store.py` | 15 | JobStore CRUD, purge rétention |
| `test_opencode_runner.py` | 44 | opencode, parsing résumé, correction |
| `test_pipeline_service.py` | 13 | Analyse de scène, filtrage, normalisation, séparation, ordre pipeline |
| `test_quality.py` | 19 | SRT checks, lexique |
| `test_quality_deep.py` | 19 | SRT réel, rapport intégré |
| `test_stt.py` | 32 | Timestamps, segments SRT, speaker clips |
| `test_summary_generator.py` | 1 | Génération résumé rapide |
| `test_web_api.py` | 38 | Routes web, login, jobs, admin config |
| `test_web_edge_cases.py` | 50 | Erreurs API, rôles, accès jobs, pipeline |
| `test_workflow.py` | 30 | États, transitions, runner |
| `test_workflow_runner.py` | 55 | Runner, correction, résumé, genre locuteur |
| `conftest.py` | — | Fixtures pytest (app, client, admin/operator/viewer) |

---

## 12. Structure disque runtime

```
instance/
  transcrIA.db                    # Base SQLite

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

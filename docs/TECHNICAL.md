# TranscrIA — Documentation technique

## 1. Vue d'ensemble

TranscrIA est un portail guidé de transcription de réunion destiné aux utilisateurs non techniciens (secrétaires de réunion). Il orchestre le dépôt d'un fichier audio/vidéo jusqu'à la production d'un package exploitable contenant le SRT corrigé (speakers + lexique), le contexte, les participants, le lexique, le rapport qualité, le rapport de correction et les points à vérifier.

**Stack :** Python 3.11+ / Flask / SQLAlchemy (SQLite) / Jinja2 / Cohere ASR / pyannote / opencode (Qwen 35B) / Bootstrap 5

**Services externes :** dashboard-llm (port 5001, monitoring GPU), SRT Editor EASY (port 7861, correction manuelle)

**Démarrage (dev) :**
```bash
cd transcria
source venv/bin/activate
python app.py
# → http://0.0.0.0:7870
# Admin: admin / admin-change-me
```

**Scripts :** `./start.sh` (log `/var/log/transcrIA.log`, PID `/run/transcrIA.pid`), `./stop.sh`, `./status.sh`

**Tests :** 412 tests pytest collectés — `python -m pytest tests/ -q`

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
│   │   ├── models.py              # Modèle User, énumération Role, ROLE_HIERARCHY
│   │   ├── store.py               # UserStore (CRUD utilisateurs, count_users, ensure_admin)
│   │   ├── permissions.py         # Permission enum, _ROLE_PERMISSIONS, @requires()
│   │   └── routes.py              # Routes /login, /logout, /admin/users (+ inject_user_context)
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
│   │   └── converter.py           # AudioConverter (ffmpeg → WAV 16kHz mono)
│   │
│   ├── stt/                       # Speech-to-Text
│   │   ├── __init__.py
│   │   ├── cohere_transcriber.py  # CohereTranscriber (load, transcribe, segments_to_srt, offload)
│   │   ├── transcription.py       # Transcriber (pipeline Cohere + _apply_speakers)
│   │   ├── diarization.py         # DiarizerService (pyannote GPU + _extract_clips WAV)
│   │   ├── speaker_detection.py   # SpeakerDetector (detect + save_mapping)
│   │   └── summary.py             # SummaryGenerator (quick transcript + VAD Silero)
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
│   │   └── quality_report.py      # QualityReporter (9 contrôles, score /100, markdown)
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
│   │   ├── llm_backend.py         # LLMBackend
│   │   └── opencode_runner.py     # OpenCodeRunner
│   │
│   └── web/                       # Interface utilisateur
│       ├── __init__.py
│       ├── routes.py              # Routes Flask (pages + API REST)
│       └── templates/             # 9 templates Jinja2 (Bootstrap 5)
│           ├── base.html
│           ├── login.html
│           ├── index.html
│           ├── job_wizard.html
│           ├── job_result.html
│           ├── dashboard_status.html
│           ├── admin_config.html
│           ├── users.html
│           └── user_form.html
│
├── configs/                       # Prompts et lexique
│   ├── lexique_metier.txt
│   └── prompts/
│       ├── summary_prompt.txt      # Prompt résumé structuré (opencode)
│       ├── correction_prompt.txt   # Prompt correction SRT (speakers + lexique + orthographe)
│
├── tests/                         # 412 tests pytest collectés
│   ├── conftest.py                # Fixtures (app, client, admin/operator/viewer)
│   ├── test_auth.py               # 17 tests — Rôles, modèles, permissions
│   ├── test_auth_store.py         # 11 tests — CRUD utilisateurs
│   ├── test_config.py             # 14 tests — Chargement YAML, sauvegarde config, debug
│   ├── test_context.py            # 16 tests — Meeting, participants, lexique, builder
│   ├── test_edge_cases.py         # 17 tests — Cas limites contexte/exports/transitions
│   ├── test_exports.py            # 3 tests — PackageBuilder
│   ├── test_gpu.py                # 9 tests — VRAMManager
│   ├── test_integrations.py       # 12 tests — DashboardClient, SrtEditorLink, OpenCodeRunner
│   ├── test_jobs.py               # 19 tests — Job model, filesystem
│   ├── test_job_store.py          # 14 tests — JobStore CRUD, purge rétention
│   ├── test_quality.py            # 14 tests — SRTChecker, LexiconChecker
│   ├── test_quality_deep.py       # 15 tests — Tests approfondis qualité avec SRT réel
│   ├── test_stt.py                # 12 tests — CohereTranscriber (timestamps, segments), speaker clips
│   ├── test_web_api.py            # 30 tests — Routes web (login, jobs, upload, admin config)
│   ├── test_web_edge_cases.py     # 38 tests — Erreurs API, rôles, accès jobs, pipeline
│   └── test_workflow.py           # 20 tests — États, transitions, runner
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
  default_stt_model: "cohere-transcribe-03-2026"
  fallback_stt_model: "large-v3"
  cohere_model_path: "./models/cohere-asr/cohere-transcribe-03-2026"
  pyannote_model: "pyannote/speaker-diarization-community-1"

workflow:
  enable_quick_summary: true
  enable_speaker_detection: true
  enable_quality_mode: true
  enable_external_srt_editor_link: true
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

---

## 4. Modules détaillés

### 4.1 Authentification (`transcria/auth/`)

**`models.py`**
| Classe/Enum | Rôle |
|---|---|
| `Role` | Enum : `admin`, `manager`, `operator`, `viewer` |
| `ROLE_HIERARCHY` | Niveaux : VIEWER=0 → ADMIN=3 |
| `User` | Modèle SQLAlchemy (hérite `UserMixin`) |

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
| `/admin/users` | GET | `MANAGE_USERS` |
| `/admin/users/new` | GET, POST | `MANAGE_USERS` |
| `/admin/users/<id>/edit` | GET, POST | `MANAGE_USERS` |

`inject_user_context()` est un context processor Flask injectant `current_user` et `user_permissions` dans les templates.

---

### 4.2 Jobs (`transcria/jobs/`)

**`models.py`**
| Énumération | Valeurs (20 états) |
|---|---|
| `JobState` | `created`, `uploaded`, `analyzed`, `summary_running`, `summary_done`, `context_done`, `participants_done`, `lexicon_done`, `speaker_detection_running`, `speaker_detection_done`, `ready_to_process`, `transcribing`, `diarizing`, `arbitrating`, `quality_checking`, `quality_checked`, `export_ready`, `completed`, `failed`, `cancelled` |

| `WORKFLOW_STEPS` (9 étapes affichées) |
|---|
| `file` (Fichier), `analyze` (Analyse), `summary` (Résumé), `context` (Contexte), `participants` (Participants & Locuteurs), `lexicon` (Lexique), `processing` (Traitement), `quality` (Qualité), `export` (Export) |

> **Note :** `WORKFLOW_STEPS`, `WorkflowState.STEPS` et `_STEPS` dans `workflow/steps.py` sont alignés sur les mêmes 9 étapes affichées. Les locuteurs sont fusionnés dans l'étape 5 "Participants & Locuteurs".

| Classe | Colonnes |
|---|---|
| `Job` | `id` (UUID PK), `owner_id` (FK→users), `title`, `state`, `processing_mode`, `created_at`, `updated_at`, `extra_data_json`, `error_message` |

| Fonction | Description |
|---|---|
| `get_state_order(state)` | Index dans l'énumération JobState |
| `get_step_for_state(state)` | Retourne l'étape WORKFLOW_STEPS correspondante |

**`store.py`**
| Méthode | Description |
|---|---|
| `JobStore.create_job(owner_id, title)` | Création traitement |
| `JobStore.get_by_id(job_id)` | Recherche |
| `JobStore.list_for_user(user, include_all)` | ADMIN voit tout, MANAGER/OPERATOR/VIEWER voient leurs propres jobs sauf `include_all=True` |
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
Contient `_STEPS` (9 entrées, sans étape `speakers` séparée) et des helpers :
- `step_requires_upload(step_id)` : retourne True pour file, analyze, summary, participants, processing, quality, export
- `step_requires_speakers(step_id)` : retourne True pour processing, quality
- `get_step_index(step_id)` / `get_next_step_id(step_id)`

**`runner.py` — `WorkflowRunner`**
| Méthode | Description | GPU |
|---|---|---|
| `run_analyze(job, audio_path)` | ffprobe | — |
| `run_summary(job, audio_path, config)` | Cohere transcription → pyannote si activé → opencode/Qwen résumé | GPU 0 → GPU 0+1 |
| `run_speaker_detection(job, audio_path, config)` | pyannote diarization + formatage | GPU 0 |
| `run_transcription(job, audio_path, config)` | Cohere ASR → segments → apply_speakers → SRT | GPU 0 |
| `run_diarization(job, audio_path, config)` | pyannote speaker mapping | GPU 0 |
| `run_correction(job, config)` | opencode+Qwen 35B : correction speakers+lexique+orthographe | GPU 0+1 |
| `run_quality_checks(job, config)` | 9 contrôles qualité | — |
| `build_export(job, config)` | Package ZIP | — |

Pipeline de traitement complet (`api_process`) :
```
run_transcription → run_diarization (si quality) → run_correction → run_quality_checks → build_export
```

Cycle de vie GPU dans `run_summary` :
```
Phase 1: ensure_free(6 Go) → GPU → Cohere ASR → offload
Phase 1b: pyannote (si enable_speaker_detection) → sauvegarde speaker_count + diarization_context.md
Phase 2: free_all_gpus() → launch_qwen_35b() → opencode run → stop_qwen_35b()
```

Cycle de vie GPU dans `run_correction` :
```
free_all_gpus() → launch_qwen_35b() → opencode run → stop_qwen_35b()
```

---

### 4.4 Audio (`transcria/audio/`)

**`analyzer.py` — `AudioAnalyzer`** (méthodes de classe)
| Méthode | Description |
|---|---|
| `analyze(file_path)` | Appelle ffprobe, retourne dict avec duration_seconds, codec, channels, sample_rate_hz, needs_conversion, estimated_fast_minutes, estimated_quality_minutes, size_bytes, format |
| `_needs_conversion(info)` | Vérifie si codec ≠ PCM 16-bit LE, channels ≠ 1, sample_rate ≠ 16000 |
| `_estimate_time(info, fast)` | Estimation temps de traitement (×0.15 rapide, ×0.30 qualité) |
| `_format_duration(seconds)` / `format_estimate(info)` | Formatage humain de l'estimation (`1h04`, `12min30s`) |

**`converter.py` — `AudioConverter`** (méthodes de classe)
| Méthode | Description |
|---|---|
| `convert_to_wav_mono_16k(input_path, output_path)` | ffmpeg → PCM 16kHz mono, timeout 300s |

---

### 4.5 STT (`transcria/stt/`)

**`cohere_transcriber.py` — `CohereTranscriber`**
| Méthode/Propriété | Description |
|---|---|
| `__init__(model_path, device)` | Initialise avec chemin modèle et device (`cuda:0` par détection auto) |
| `available` | Vérifie torch + transformers importables |
| `load()` | Charge `CohereTranscribeModel` via `AutoModelForSpeechSeq2Seq` + `AutoProcessor` (trust_remote_code=True, bfloat16) |
| `transcribe(audio_path, language, chunk_length_s, progress_callback)` | Segmentation en chunks de 30s → inférence → segments [{start, end, text}] |
| `segments_to_srt(segments, speaker_map)` | Conversion segments → SRT standard avec préfixe speaker |
| `offload()` | Libère modèle + processor + gc + cuda.empty_cache |
| `_seconds_to_srt_time(seconds)` | Conversion timestamp SRT (HH:MM:SS,mmm) |

Constantes : `_COHERE_MODEL_REPO = "CohereLabs/cohere-transcribe-03-2026"`, `_SUPPORTED_LANGUAGES` (14 langues)

**`transcription.py` — `Transcriber`**
| Méthode | Description |
|---|---|
| `__init__(config, gpu_index)` | Initialise avec config et gpu_index |
| `transcribe(job, audio_path)` | Cohere → sauvegarde segments.json → applique speaker_map si available → sauvegarde SRT |
| `_apply_speakers(segments, speaker_turns, speaker_mapping)` | Overlap speaker-to-segment : pour chaque segment, trouve le turn avec le plus grand overlap |

> **Note :** `Transcriber.transcribe()` sauvegarde `metadata/speakers_map.json` avec `speaker_map = speaker_mapping or {}`.

**`diarization.py` — `DiarizerService`**
| Méthode | Description |
|---|---|
| `__init__(config, device)` | Initialise avec config et device |
| `available` | Vérifie `pyannote.audio` importable |
| `diarize(job, audio_path)` | Charge pipeline pyannote → inférence → turns [{start, end, speaker, duration}] → stats → sauvegarde |
| `_extract_clips(audio_path, turns, speakers, fs)` | Extrait des extraits WAV par locuteur (3 clips, 3-12s) |
| `_load_audio_gpu(audio_path, device)` | torchaudio → resample 16kHz → tensor GPU |

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
| `generate_quick_summary(job, audio_path, gpu_index)` | Charge Cohere → transcrit → sauvegarde quick_transcript.txt et summary.json → retourne dict |
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
| `save(job, jobs_dir, terms)` | Valide (strip, id auto, defaults) et sauvegarde JSON + .txt |
| `import_from_file(job, jobs_dir, content)` | Import CSV ou liste simple (# = commentaire) |
| `load_global_lexicon(config)` | Charge configs/lexique_metier.txt |

`LEXICON_CATEGORIES` : personne, application, sigle, projet, service, métier, médical, technique, lieu, autre
`LEXICON_PRIORITIES` : critique, importante, normale

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
9 contrôles systématiques :
1. Segments vides (texte vide)
2. Segments très courts (< 0.5s)
3. Segments très longs (> 60s)
4. Trous temporels (> 5s)
5. Chevauchements (end > next start)
6. Locuteurs non mappés (SPEAKER_XX)
7. Termes normalisés du lexique absents (`replace_by`) dans le SRT corrigé
8. Couverture audio (< 80%)
9. Ratio mots/seconde suspect (< 0.5 ou > 10)

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
- `services.stop_script` (défaut `./scripts/stop_qwen.sh`)
- `services.qwen_port` (8080), `services.vllm_port` (8000)
- `gpu.cohere_vram_mb`, `gpu.pyannote_vram_mb`, `gpu.llm_vram_mb`, `gpu.min_free_vram_mb`

| Méthode | Description |
|---|---|
| `get_gpu_info()` | Dashboard API ou fallback PyTorch |
| `get_free_vram_mb(gpu_index)` | VRAM libre en Mo |
| `get_best_gpu(required_mb)` | Meilleur GPU disponible (≥ required + MIN_FREE) |
| `ensure_free(required_mb, preferred_gpu)` | Vérifie/libère/alloue GPU (SIGTERM puis SIGKILL si >4Go) |
| `_free_memory(gpu_index)` | Kill processus GPU > 4Go (SIGTERM puis SIGKILL après 2s) |
| `stop_vllm_port_8000()` | Tue vLLM sur port 8000 via _kill_port |
| `launch_qwen_35b()` | Lance `services.arbitrage_script` (défaut `scripts/launch_arbitrage.sh`) → attend port Qwen (timeout 600s) |
| `stop_qwen_35b()` | Tue processus sur port 8080 |
| `free_all_gpus()` | stop_vllm + stop_qwen + offload_all + sleep 2s |
| `is_port_open(port)` | Vérifie `/v1/models` accessible + teste une inférence réelle `/v1/completions` |
| `_wait_for_port(port, timeout)` | Boucle d'attente avec `is_port_open` toutes les 5s |
| `track_model / untrack_model` | Enregistre/désenregistre un modèle chargé |
| `offload_all()` | Vide `_loaded_models` + gc + cuda.empty_cache |

**`opencode_runner.py` — `OpenCodeRunner`**

Constantes : `OPENCODE_BIN = "opencode"`, `PROVIDER = "local"`, `MODEL = "qwen3-35b-arbitrage"`

| Méthode | Description |
|---|---|
| `__init__(work_dir, model, provider)` | Initialise avec répertoire de travail et modèle |
| `run(instruction, prompt_file, timeout)` | Lance `opencode run --format json --model {provider}/{model}` via `subprocess.run` → parse NDJSON → retourne {success, output, files, events_count, tool_calls} |
| `run_summary(transcript_path, context_path, diarization_context_path)` | Génère un résumé structuré via opencode + Qwen 35B. Inclut la diarization acoustique si disponible, lit le fichier summary.md produit et le parse |
| `run_correction(srt_path, context_path, lexicon_path)` | Correction SRT : lit transcription.srt + job_context.yaml + session_lexicon.json, écrit transcription_corrigee.srt + correction_report.md |
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
| `/` | GET | login_required | Accueil (liste des traitements) |
| `/jobs/new` | POST | login_required + CREATE_JOBS | Création traitement |
| `/jobs/<id>` | GET | login_required + owner check | Assistant wizard 9 étapes |
| `/jobs/<id>/result` | GET | login_required + owner check | Page résultat |
| `/jobs/<id>/delete` | POST | login_required + DELETE_JOBS | Suppression traitement |
| `/system` | GET | login_required + ACCESS_SYSTEM | État technique (GPU dashboard) |
| `/admin/config` | GET, POST | login_required + MANAGE_CONFIG | Édition YAML de la configuration |
| `/api/jobs/<id>/upload` | POST | login_required + owner/admin check | Upload fichier audio |
| `/api/jobs/<id>/analyze` | POST | login_required + owner/admin check | Analyse ffprobe |
| `/api/jobs/<id>/summary` | POST | login_required + owner/admin check | Résumé rapide |
| `/api/jobs/<id>/context` | POST | login_required + owner/admin check | Sauvegarde contexte |
| `/api/jobs/<id>/participants` | POST | login_required + owner/admin check | Sauvegarde participants |
| `/api/jobs/<id>/lexicon` | POST | login_required + owner/admin check | Sauvegarde lexique |
| `/api/jobs/<id>/speakers/detect` | POST | login_required + owner/admin check | Détection locuteurs |
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
| `/api/system/status` | GET | `ACCESS_SYSTEM` | État système JSON |

**Templates** (`web/templates/`)
| Template | Description |
|---|---|
| `base.html` | Layout principal (navbar Bootstrap 5, flash messages, permissions) |
| `login.html` | Page de connexion |
| `index.html` | Accueil : liste des traitements + bouton nouveau |
| `job_wizard.html` | Assistant 9 étapes avec formulaires interactifs (JS fetch API) |
| `job_result.html` | Résultat : SRT, qualité, exports, lien SRT Editor |
| `admin_config.html` | Éditeur YAML de configuration admin |
| `users.html` | Liste des utilisateurs (admin) |
| `user_form.html` | Formulaire création/édition utilisateur |
| `dashboard_status.html` | État technique (GPU, CPU, RAM, services) |

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
5. Résumé        → Cohere ASR transcrit → pyannote détecte locuteurs → opencode+Qwen résume
6. Contexte      → formulaire pré-rempli avec suggestions IA (titre, type, sujet, objectif)
7. Participants & Locuteurs → SPEAKER avec bouton écoute + champs nom/fonction/rôle pré-remplis
8. Lexique       → termes suspects détectés par l'IA, champ "Remplacer par" pré-rempli
9. Traitement    → Cohere ASR → correction opencode+Qwen (speakers+lexique) → qualité → export
```

### Pipeline de traitement détaillé

```
Étape 5 - Résumé :
  Phase 1 : ensure_free(6 Go) → GPU → Cohere ASR (transcription 30s chunks) → offload
  Phase 1b: pyannote (si enable_speaker_detection) → speaker_count_pyannote + summary/diarization_context.md
  Phase 2 : free_all_gpus() → launch_qwen_35b() → opencode run_summary avec diarization_context si disponible → parse résultats → stop_qwen_35b()
  → meeting_context enrichi (title_suggere, type_suggere, sujet_suggere, participants_detectes, termes_suspects, summary_llm)

Étape 9 - Traitement (mode quality) :
  Cohere ASR (GPU 0, ensure_free 6Go) → _apply_speakers → sauvegarde SRT et segments
  → pyannote diarization (GPU 0)
  → opencode+Qwen correction SRT (GPU 0+1, free_all + launch_qwen_35b)
  → qualité (9 checks, score /100)
  → export ZIP

Étape 9 - Traitement (mode fast) :
  Cohere ASR → correction opencode → qualité → export
  (pas de diarization supplémentaire)
```

---

## 7. Gestion GPU

### 7.1 Modèles disponibles

| Modèle | GPU | VRAM | Lancement | Port |
|---|---|---|---|---|
| Cohere ASR (2B) | 1 GPU | ~5-6 Go | Python (transformer) | — |
| pyannote community-1 | 1 GPU | ~2 Go | Python (pyannote.audio) | — |
| Qwen 3.6 35B (actuel) | variable (selon script/config machine) | variable | `launch_arbitrage.sh` (llama.cpp) | 8080 |
| vLLM (optionnel) | variable | variable | externe au repo | 8000 |

Le backend d'arbitrage fourni dans ce repo est **llama.cpp** (`llama-server`) via `scripts/launch_arbitrage.sh`.
Le modèle actuellement utilisé est `Qwen3.6-35B-A3B-UD-Q8_K_XL.gguf`, mais il est modifiable (chemin modèle et paramètres du script, `workflow.*.model_id`, provider opencode).

Observation machine (17 mai 2026) avec la LLM d'arbitrage active :
- `launch_arbitrage.sh` utilise `--tensor-split 1,1,1` (répartition sur 3 GPUs sur cette machine).
- `nvidia-smi` montre `llama-server` chargé sur GPU 0/1/2 avec ~18.7 GiB / ~15.6 GiB / ~15.7 GiB.
- Cette empreinte dépend du quantization, des flags (`ctx-size`, cache KV, etc.) et de la topologie GPU.

### 7.2 Cycle de vie automatique

```
Transcription (Phase 1):
  ensure_free(6 Go) → GPU trouvé → charge Cohere → transcrit → offload

Résumé LLM (Phase 2):
  free_all_gpus() → stop vLLM 8000 → launch_qwen_35b → attend port 8080
  → opencode run → summary → stop_qwen_35b()

Correction SRT (Phase 3):
  free_all_gpus() → launch_qwen_35b → opencode run → stop_qwen_35b()
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

**412 tests collectés** couvrant tous les modules. Lancer avec :
```bash
cd transcria && python -m pytest tests/ -v
```

Organisation :
| Fichier | Tests | Couverture |
|---|---|---|
| `test_auth.py` | 17 | Rôles, modèles, permissions, décorateur |
| `test_auth_store.py` | 11 | CRUD utilisateurs |
| `test_config.py` | 14 | Chargement YAML, sauvegarde config, env var, debug |
| `test_context.py` | 16 | Meeting, participants, lexique, builder |
| `test_edge_cases.py` | 17 | Cas limites, transitions workflow |
| `test_exports.py` | 3 | PackageBuilder |
| `test_gpu.py` | 9 | VRAMManager |
| `test_integrations.py` | 12 | Dashboard, SRT Editor, OpenCodeRunner |
| `test_jobs.py` | 19 | Job model, filesystem |
| `test_job_store.py` | 14 | JobStore CRUD, purge rétention |
| `test_quality.py` | 14 | SRT checks, lexique |
| `test_quality_deep.py` | 15 | SRT réel, rapport intégré |
| `test_stt.py` | 12 | Timestamps, segments SRT, speaker clips |
| `test_web_api.py` | 30 | Routes web, login, jobs, admin config |
| `test_web_edge_cases.py` | 38 | Erreurs API, rôles, accès jobs, pipeline |
| `test_workflow.py` | 20 | États, transitions, runner |
| `conftest.py` | — | Fixtures pytest (app, client, admin/operator/viewer) |

---

## 12. Structure disque runtime

```
instance/
  transcrIA.db                    # Base SQLite

jobs/{uuid}/
  input/original.{ext}           # Fichier audio original
  metadata/
    audio_analysis.json           # Résultat ffprobe
    transcription.srt             # SRT brut (Cohere)
    transcription_corrigee.srt   # SRT corrigé (opencode)
    transcription_segments.json  # Segments détaillés
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
    speaker_turns.json            # Turns pyannote
    speaker_stats.json            # Stats par locuteur
    speaker_mapping.json          # Mapping SPEAKER_XX → participant
    speaker_clips.json            # Liste des extraits audio
    samples/                      # Fichiers WAV extraits
  quality/
    quality_report.json           # Rapport qualité structuré
    quality_report.md             # Rapport qualité markdown
    review_points.json            # Points à vérifier
  exports/
    transcrIA_job_{uuid}.zip       # Package final
```

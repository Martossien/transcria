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

# Lancer l'application (production — service systemd)
sudo systemctl restart transcria.service   # redémarre proprement
sudo systemctl stop transcria.service
sudo systemctl status transcria.service
sudo truncate -s 0 /var/log/transcrIA.log  # remet le log à zéro (débogage)

# Scripts legacy (si systemd non disponible)
./start.sh    # log: /var/log/transcrIA.log, PID: /run/transcrIA.pid
./stop.sh
./status.sh

# Tests
python -m pytest tests/ -q           # 445 tests collectés (21 modules test_*.py + E2E, mock, pas de GPU requis)
python -m pytest tests/test_auth.py -v
# ⚠️  Tests E2E : TOUJOURS utiliser le python du venv (pyannote et Cohere n'y sont que là)
venv/bin/python tests/test_e2e_workflow.py --skip-llm               # E2E rapide (1 GPU)
venv/bin/python tests/test_e2e_workflow.py                          # E2E complet (GPUs + LLM requis)
venv/bin/python tests/test_e2e_workflow.py --keep                   # Conserve le job pour inspection
venv/bin/python tests/test_e2e_workflow.py --audio tests/test2.mp3  # Autre fichier audio

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
    workflow/
      states.py             # WorkflowState.compute_statuses()
      steps.py              # WORKFLOW_STEPS (9 étapes)
      runner.py             # WorkflowRunner — exécution des étapes
      transitions.py        # logique lancement / annulation / reprise
    audio/
      analyzer.py           # AudioAnalyzer (ffprobe)
      converter.py          # AudioConverter (ffmpeg)
      vad.py                # SileroVAD — détection de parole via faster_whisper
    stt/
      base_transcriber.py   # BaseTranscriber (ABC)
      cohere_transcriber.py # CohereTranscriber — Cohere ASR (AutoModelForSpeechSeq2Seq, numpy array)
      whisper_transcriber.py# WhisperTranscriber — faster-whisper large-v3
      transcriber_factory.py# TranscriberFactory — sélection backend selon config
      transcription.py      # Transcriber — chunking par tours pyannote ou 30s fixe
      diarization.py        # DiarizerService — pyannote + exclusive_speaker_diarization
      speaker_detection.py  # SpeakerDetector
      summary.py            # SummaryGenerator — VAD pré-transcription + Cohere
    context/
      meeting_context.py    # MeetingContextManager
      participants.py       # ParticipantsManager
      lexicon.py            # LexiconManager (20 catégories, variants, contexts)
      job_context_builder.py# JobContextBuilder — assemble job_context.yaml/json
    quality/
      quality_report.py     # QualityReporter (10 checks, score /100)
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
  tests/                    # 21 modules test_*.py + E2E, 445 tests collectés (mocks GPU/LLM)
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
3. **LLM d'arbitrage locale** : VRAM variable selon modèle/backend/script (ex: 48–60 Go pour un 35B quantifié)

**`GPUSession`** est le context manager utilisé pour Cohere et pyannote. Il appelle `ensure_free()` → scanne tous les GPUs → sélectionne le meilleur (VRAM libre max) → logue le GPU choisi → libère via `offload_all()` à la sortie. Ne pas hardcoder `cuda:0` — utiliser `GPUSession` ou `ensure_free()`.

**`ensure_arbitrage_llm_ready(expected_model_id)`** est le point d'entrée unique avant tout usage de la LLM d'arbitrage. Elle vérifie l'état réel du serveur (`/v1/models` + inférence test) et choisit parmi trois chemins logués explicitement :
- **CAS A** : LLM active et bon modèle → réutilisation directe, zéro redémarrage
- **CAS B** : LLM active mais mauvais modèle → redémarrage (warning logué)
- **CAS C** : LLM absente ou non saine → libération GPU + lancement depuis zéro

**Cycle de vie LLM** : chaque étape appelle uniquement `ensure_arbitrage_llm_ready()`. L'arrêt (`stop_arbitrage_llm()`, alias legacy `stop_qwen_35b()`) est fait **une seule fois** en fin de pipeline par `PipelineService._release_arbitrage_llm()`, qui vérifie d'abord `is_arbitrage_llm_running()` avant d'agir. Ainsi la LLM reste vivante entre le résumé et la correction (CAS A garanti pour la correction si le résumé l'a démarrée).

`services.arbitrage_api_model_id` dans `config.yaml` doit correspondre à l'alias rapporté par le serveur (lancer `scripts/check_arbitrage_llm.sh` pour vérifier). `services.arbitrage_llm_port` remplace `qwen_port` pour les nouvelles configs. `services.llm_cleanup_ports` remplace `vllm_port` et liste les ports de backends LLM concurrents à libérer avant lancement. Les anciens noms restent lus par compatibilité. `free_all_gpus()` reste disponible pour les resets forcés uniquement.

Les références `qwen_*` encore présentes sont des aliases de compatibilité ancienne version ou des exemples de modèle local. Ne pas introduire de nouvelle dépendance fonctionnelle au nom Qwen : le contrat applicatif est "LLM d'arbitrage OpenAI-compatible configurée".

### Pipeline STT — deux modes de chunking

**Mode pyannote_turns (prioritaire) :** si `speaker_turns.json` contient `exclusive_turns` (produit par la phase summary), `Transcriber.transcribe()` charge l'audio en mémoire une seule fois, découpe par tours pyannote, et passe des `np.ndarray` directement à `CohereTranscriber.transcribe()`. Chaque chunk a un speaker connu → attribution 100% fiable, pas d'overlap matching.

**Mode 30s_fallback :** si `exclusive_turns` est absent (premier run ou pyannote indisponible), chunking 30s fixe suivi de `_apply_speakers()` (overlap matching). Comportement identique à l'implémentation pré-refactoring.

**VAD Silero :** `SummaryGenerator` utilise `SileroVAD` (via `faster_whisper`) pour ne soumettre à Cohere que les zones de parole détectées en phase résumé (`workflow.vad.enabled_summary=true`). La transcription finale garde le VAD désactivé par défaut (`workflow.vad.enabled_final=false`) car les tours pyannote servent déjà de VAD implicite et le filtrage VAD final peut dégrader Cohere sur certains sons. Fallback transparent si `faster_whisper` est indisponible (chunking 30s).

**`CohereTranscriber.transcribe()` accepte deux formes d'entrée :**
- `transcribe(audio_path=Path(...))` — charge l'audio depuis le disque (usage standard)
- `transcribe(audio_path=None, audio_array=np.ndarray, sample_rate=16000)` — audio déjà en mémoire (chunking par tours, évite les I/O)

### Workflow (9 étapes affichées)
Le wizard guide l'utilisateur de l'upload au package ZIP. Chaque étape correspond à un `JobState`. Les transitions passent obligatoirement par `workflow/transitions.py`. Voir `docs/DATA_MODEL.md` pour le détail des états.

### Modèle service/worker
`/api/jobs/<id>/process` planifie le traitement ; `JobExecutorService` l'exécute en arrière-plan (worker sérialisé, `workflow.execution.max_concurrent_jobs=1`). Supervision : `/health`, `/ready`, `/metrics`.

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

### opencode — provider `local` requis dans `~/.opencode/config.json`
`OpenCodeRunner` invoque opencode avec `--model local/qwen3-35b-arbitrage`. Dans opencode, le préfixe `local/` désigne un provider nommé `local`. Ce provider **doit** être déclaré dans `~/.opencode/config.json` avec un `baseUrl` pointant sur le serveur llama.cpp (port 8080 par défaut). Sans cette entrée, opencode ne sait pas résoudre `local/` → les appels LLM échouent silencieusement et `summary.md` conserve le placeholder. Exemple minimal :
```json
{
  "providers": {
    "local": {
      "type": "openai",
      "baseUrl": "http://127.0.0.1:8080/v1",
      "apiKey": "sk-no-key-required",
      "models": ["qwen3-35b-arbitrage"],
      "validate": false
    }
  }
}
```
`validate: false` est nécessaire car le nom court (`qwen3-35b-arbitrage`) diffère de l'alias complet rapporté par llama-server (`qwen3-35b-arbitrage-ud-q8_k_xl`). llama.cpp ignore le `model` dans la requête et utilise le modèle chargé.

### `correction_prompt.txt` — version courante : v1.9

**summary_prompt.txt v2.0 (2026-05-19)** : restructuration complète. Points critiques pour la compatibilité parser : section `## Participants probables` (match exact), section `## Termes douteux à valider` (match `## Termes (?:suspects|douteux).*?`), format terme `**TERME** [cat] (prio) | variantes_suspectes: ... | commentaire: ... | contextes: ...`, `(aucune)` filtré par `empty_markers`, séparateur `||` pour contextes multiples (`_parse_summary_contexts`), `(non identifiable)` pour participants absents.

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
`SileroVAD` est utilisé en pré-transcription par `SummaryGenerator` si `workflow.vad.enabled_summary=true`, et peut être utilisé en post-filtrage par `Transcriber._apply_vad_filter()` si `workflow.vad.enabled_final=true`. Si `faster_whisper` n'est pas installé, `SileroVAD.available` retourne `False` et les appelants basculent en chunking 30s fixe sans erreur. Ne pas supposer que VAD est toujours actif. Les paramètres `threshold`, `min_speech_duration_ms`, `min_silence_duration_ms`, `speech_pad_ms` sont configurables dans `workflow.vad`.

### Qualité SRT — garde-fous déterministes
`QualityReporter` signale maintenant une charge de relecture (`review_load`) avec noms de locuteurs modifiés, segments marqués étrangers, segments non latins et segments courts suspects. Les marqueurs courts de bruit ASR sont configurables via `quality.asr_noise_markers`; ne pas ajouter de phrases métier ou de cas client dans le code pour ces heuristiques.

### tests/ couvre le métier, moins les intégrations GPU
445 tests collectés dans 21 modules `test_*.py` (plus E2E) couvrent stores, config, contexte, qualité, exports, routes Flask et workflow. La plupart mockent les dépendances GPU/LLM. `test_e2e_workflow.py` requiert un vrai GPU.

### E2E : utiliser impérativement `venv/bin/python`, pas `python`
Le Python système (3.13, `/usr/bin/python`) n'a pas accès aux packages du venv (`pyannote`, `torch`, `cohere_transcriber`). Lancer `python tests/test_e2e_workflow.py` depuis le système donne « pyannote non disponible » silencieusement. Toujours utiliser `venv/bin/python tests/test_e2e_workflow.py` ou activer le venv au préalable (`source venv/bin/activate`).

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
| `docs/VAD_PYANNOTE_PISTES.md` | VAD Silero : avantages/inconvénients, métriques de détection de dégradation, tuning pyannote, pistes d'amélioration |

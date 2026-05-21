# TranscrIA — Référence de configuration (config.yaml)

## Vue d'ensemble

La configuration est chargée depuis `config.yaml` (ou le chemin dans la variable d'environnement `TRANSCRIA_CONFIG`). Le mécanisme de chargement :

1. `load_config()` part de `_DEFAULT_CONFIG` (valeurs hardcodées dans `transcria/config/loader.py`)
2. Si le fichier YAML existe, il est chargé et fusionné récursivement via `_deep_merge()`
3. `get_config()` retourne un singleton — première appel charge, appels suivants réutilisent
4. `save_config(cfg)` écrit un YAML sur disque (`TRANSCRIA_CONFIG` si défini, sinon `config.yaml`) en normalisant les valeurs non supportées
5. `set_config(cfg)` met à jour le singleton en mémoire après sauvegarde/rechargement
6. Les modules qui capturent une config passée au constructeur ne voient pas forcément les mises à jour tant qu'ils ne sont pas réinstanciés

### Fichiers de configuration

| Fichier | Rôle | Dans git ? |
|---|---|---|
| `config.example.yaml` | Template pour nouveau déploiement | Oui |
| `config.yaml` | Configuration de production | **Non** (chemins absolus, secrets) |
| `_DEFAULT_CONFIG` dans `transcria/config/loader.py` | Valeurs par défaut si YAML absent | Dans le code |

### Différences connues config.example.yaml vs config.yaml production

| Paramètre | `config.example.yaml` | `config.yaml` (production) |
|---|---|---|
| `models.cohere_model_path` | `./models/cohere-asr/...` (relatif) | peut être absolu selon l'installation |
| `workflow.summary_llm.model_id` | `local/votre-modele-llm-ici` | identifiant du modèle utilisé |
| `workflow.summary_llm.timeout_seconds` | 1800 | typ. 1800+ |
| `workflow.summary_llm.use_chat_api` | absent | `true` |

La clé `qwen_port` reste lue pour compatibilité avec les anciennes installations (alias de `arbitrage_llm_port`). Les nouvelles configurations doivent utiliser `arbitrage_llm_port`, `stop_arbitrage_llm.sh` et `llm_cleanup_ports`. Les méthodes Python `launch_qwen_35b()` et `stop_qwen_35b()` ont été supprimées — utiliser `launch_arbitrage_llm()` et `stop_arbitrage_llm()`.

---

## Sections et paramètres

### `server`

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `host` | string | `"0.0.0.0"` | Hôte d'écoute Flask |
| `port` | int | `7870` | Port d'écoute Flask |
| `debug` | bool | `true` | Mode debug Flask (rechargement auto, stack traces détaillées) |

**Redémarrage requis :** oui pour tous les paramètres (Flask les lit au démarrage dans `main()`).

**Surcharge CLI :**
```bash
python app.py --host 127.0.0.1 --port 8080 --debug
python app.py --no-debug
# ou variables d'environnement : TRANSCRIA_HOST, TRANSCRIA_PORT, TRANSCRIA_DEBUG
```

**Sécurité :** `debug=true` en production expose les stack traces. Le port par défaut 7870 est choisi pour ne pas entrer en conflit avec le dashboard (5001) et SRT Editor (7861).

---

### `storage`

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `jobs_dir` | string | `"./jobs"` | Répertoire racine des données de jobs (chemin relatif ou absolu) |
| `database_url` | string | `"sqlite:///transcrIA.db"` | URL SQLAlchemy (SQLite par défaut) |

**Redémarrage requis :** oui pour `database_url`. `jobs_dir` est relu par `JobFilesystem` à chaque opération (pas de cache).

**Impact si modifié :**
- `jobs_dir` : les jobs existants ne sont PAS déplacés. Si le chemin change, les anciens jobs sont "perdus" (fichiers toujours sur disque mais base orpheline de ces fichiers).
- `database_url` : la base est initialisée une seule fois au démarrage (`db.create_all()`). Changer cette URL nécessite de migrer la base manuellement.

---

### `auth`

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Toujours normalisé à `true` : le mode sans authentification n'est pas supporté |
| `first_admin_username` | string | `"admin"` | Login du premier admin créé si la base est vide |
| `first_admin_password` | string | `"admin-change-me"` | Mot de passe du premier admin |

**Redémarrage requis :** non pour le premier admin (lu une seule fois si la base est vide). `enabled=false` est ignoré et réécrit en `true` par `load_config()` / `save_config()`.

**Sécurité :** `first_admin_password` est stocké dans le YAML et n'est utilisé que si `UserStore.count_users() == 0` (base vide). Après la création du premier admin, le changer dans le YAML n'a aucun effet. Dans l'éditeur `/admin/config`, ce champ est masqué avec `********` et la valeur existante est préservée si la sentinelle est resoumise.

---

### `services`

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `dashboard_llm_url` | string | `"http://127.0.0.1:5001"` | URL du dashboard LLM (monitoring GPU) |
| `srt_editor_easy_url` | string | `"http://127.0.0.1:7861"` | URL de SRT Editor EASY |
| `arbitrage_api_model_id` | string | — | Model ID rapporté par `/v1/models` (alias `--alias` du script llama-server). Doit correspondre exactement pour activer la réutilisation sans redémarrage (CAS A). Lancer `scripts/check_arbitrage_llm.sh` pour obtenir la valeur. |

**Redémarrage requis :** non — ces URLs sont lues dynamiquement par `VRAMManager.__init__()` et les templates.

**Impact si modifié :**
- `dashboard_llm_url` : utilisé par `VRAMManager` pour interroger l'API GPU (`/api/v1/gpus`). Si le dashboard est indisponible, `VRAMManager` bascule sur `nvidia-smi`.
- `srt_editor_easy_url` : utilisé pour le bouton "Ouvrir dans SRT Editor" et l'API `push-to-editor`. Si l'URL est incorrecte, le bouton apparaît mais la redirection échoue.

---

### `models`

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `stt_backend` | string | `"cohere"` | Backend STT (`cohere` ou `whisper`) |
| `default_stt_model` | string | `"cohere-transcribe-03-2026"` | Modèle STT par défaut |
| `fallback_stt_model` | string | `"large-v3"` | Modèle fallback |
| `cohere_model_path` | string | `"./models/cohere-asr/cohere-transcribe-03-2026"` | Chemin vers le modèle Cohere ASR local |
| `pyannote_model` | string | `"pyannote/speaker-diarization-community-1"` | Nom du modèle pyannote HuggingFace |

**Redémarrage requis :** non — les chemins sont lus à chaque transcription/diarization.

**Impact si modifié :**
- `cohere_model_path` : si le chemin est invalide, `CohereTranscriber.load()` échoue avec un avertissement. Le chemin est résolu en absolu si c'est un répertoire local (`os.path.abspath`). Si le chemin commence par `CohereLabs/` ou `cohere/`, HuggingFace download est utilisé.
- `pyannote_model` : doit être un modèle HuggingFace valide. Nécessite d'accepter les conditions sur huggingface.co et configurer `HF_TOKEN` pour les modèles gated.
- `stt_backend` pilote la sélection du backend via `TranscriberFactory`.

### `whisper`

Paramètres du backend Whisper qualité (`faster-whisper`, Large V3 par défaut).
Ces réglages sont utilisés quand `models.stt_backend=whisper`, et le mode qualité
peut les activer automatiquement via `workflow.quality_transcription`.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `model_size` | string | `"large-v3"` | Modèle faster-whisper |
| `compute_type` | string | `"float16"` | Type de calcul CTranslate2 (`float16` GPU recommandé, `int8` CPU) |
| `cpu_threads` | int | `4` | Threads CPU |
| `chunk_length_s` | int | `30` | Taille de chunk Whisper |
| `beam_size` | int | `5` | Beam search |
| `best_of` | int | `5` | Nombre de candidats |
| `vad_filter` | bool | `true` | VAD interne faster-whisper |
| `word_timestamps` | bool | `true` | Timestamps mot-à-mot natifs faster-whisper |
| `condition_on_previous_text` | bool | `false` | Désactivé pour limiter les boucles/hallucinations |
| `no_speech_threshold` | float/null | `0.2` | Seuil non-parole |
| `compression_ratio_threshold` | float/null | `2.0` | Détection de texte compressé/répétitif |
| `log_prob_threshold` | float/null | `-1.0` | Seuil log-probabilité |
| `hallucination_silence_threshold` | float/null | `3.0` | Coupure hallucinations sur silence |
| `repetition_penalty` | float | `1.0` | Pénalité de répétition |
| `no_repeat_ngram_size` | int | `0` | Interdiction de n-grammes répétés |
| `suppress_numerals` | bool | `false` | Supprime chiffres/symboles pendant l'ASR pour faciliter l'alignement CTC ; désactivé par défaut pour préserver les nombres |
| `hotwords` | string/null | `null` | Mots-clés/hints pour termes rares |
| `initial_prompt` | string/null | `null` | Prompt initial Whisper |
| `collapse_repetition_loops` | bool | `true` | Réduit les boucles textuelles répétées après ASR |
| `repetition_loop_min_repeats` | int | `4` | Nombre minimum de répétitions consécutives suspectes |
| `repetition_loop_max_phrase_words` | int | `10` | Taille maximale d'une phrase répétée détectée |
| `repetition_loop_keep_repeats` | int | `2` | Occurrences conservées après réduction d'une boucle |

#### `whisper.forced_alignment`

Alignement forcé CTC optionnel via torchaudio. Il est désactivé par défaut pour
éviter tout téléchargement de modèle non demandé ; si le backend CTC n'est pas
disponible, TranscrIA conserve les timestamps faster-whisper sans échec.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Active l'alignement CTC optionnel |
| `backend` | string | `torchaudio_ctc` | Backend d'alignement natif supporté |
| `bundle_name` | string/null | `VOXPOPULI_ASR_BASE_10K_FR` | Bundle torchaudio utilisé pour l'alignement |
| `max_segment_s` | float | `30.0` | Durée maximale d'un segment aligné |

---

### `workflow`

Paramètres contrôlant les fonctionnalités du workflow.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `enable_quick_summary` | bool | `true` | Active l'étape Résumé (transcription Cohere rapide + LLM) |
| `enable_speaker_detection` | bool | `true` | Active la détection pyannote des locuteurs |
| `enable_quality_mode` | bool | `true` | Active le mode "Qualité" (diarization finale + correction SRT) |
| `enable_external_srt_editor_link` | bool | `true` | Affiche le bouton "Ouvrir dans SRT Editor EASY" |
| `enable_vad` | bool | `true` | Ancien interrupteur global VAD, conservé pour compatibilité |

#### `workflow.quality_transcription`

Contrôle le basculement vers le backend STT de qualité. Par défaut, Cohere reste
le backend principal, mais Whisper large-v3 est utilisé en mode qualité ou si le
résumé rapide a diagnostiqué un son dégradé.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `force_stt_backend` | string/null | `whisper` | Backend forcé quand une règle qualité s'applique |
| `enabled_for_modes` | list[string] | `["quality"]` | Modes de traitement qui forcent le backend qualité |
| `force_on_degraded_summary` | bool | `true` | Force le backend qualité si `summary/summary.json` signale un niveau dégradé |
| `degraded_summary_levels` | list[string] | `["degrade"]` | Niveaux de diagnostic considérés comme dégradés |

#### `workflow.audio_quality`

Agrège les signaux ffprobe et les diagnostics du résumé rapide pour décider si
le backend qualité doit être forcé, même hors mode qualité.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `force_quality_backend` | bool | `true` | Autorise le forçage Whisper sur qualité dégradée |
| `degraded_levels` | list[string] | `["degrade"]` | Niveaux de résumé considérés dégradés |
| `suspect_levels` | list[string] | `["suspect"]` | Niveaux de résumé suspects, pondérés plus faiblement |
| `min_bit_rate` | number/null | `64000` | Bitrate minimal avant signal qualité faible |
| `min_sample_rate_hz` | number/null | `16000` | Fréquence minimale avant signal qualité faible |
| `max_non_latin_segments` | number/null | `2` | Nombre maximal de segments non latins toléré |
| `max_short_segment_ratio` | number/null | `0.2` | Ratio maximal de segments courts suspects |
| `min_speech_ratio` | number/null | `0.35` | Ratio VAD minimal avant suspicion de VAD trop agressif |
| `max_speech_ratio` | number/null | `0.95` | Ratio VAD maximal avant suspicion de VAD peu sélectif |

**Redémarrage requis :** non — ces booléens sont lus à chaque appel dans `WorkflowRunner` et les templates.

**Impact si modifié :**
- `enable_quick_summary=false` : l'étape Résumé est sautée. Le job passe directement d'ANALYZED à... rien (pas de transition prévue dans `compute_statuses`). **Casserait le workflow** car les étapes suivantes (Contexte, Participants) dépendent du résumé pour pré-remplir les suggestions.
- `enable_speaker_detection=false` : `SpeakerDetector.detect()` n'est pas appelé dans `run_summary()`. L'étape Participants n'aura pas de locuteurs pyannote, seulement les suggestions LLM (moins précises).
- `enable_quality_mode=false` : le mode "Qualité" n'est pas proposé dans le formulaire de traitement. Seul le mode "Rapide" est disponible.
- `enable_external_srt_editor_link=false` : le bouton SRT Editor est masqué dans le template.

#### `workflow.vad`

Paramètres Silero VAD. La configuration fine évite de traiter le VAD comme un interrupteur
global alors que le résumé et la transcription finale n'ont pas les mêmes risques.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `enabled_summary` | bool | `true` | Active le VAD avant la transcription rapide Cohere du résumé |
| `enabled_final` | bool | `false` | Active un filtrage VAD supplémentaire sur les chunks pyannote de la transcription finale |
| `adaptive` | bool | `true` | Ajuste les seuils VAD selon `metadata/audio_quality_decision.json` |
| `threshold` | float | `0.5` | Seuil Silero |
| `threshold_low_quality` | float | `0.35` | Seuil appliqué si audio dégradé/faible qualité |
| `threshold_high_noise` | float | `0.6` | Seuil appliqué si VAD peu sélectif |
| `min_speech_duration_ms` | int | `250` | Durée minimale de parole détectée |
| `min_silence_duration_ms` | int | `400` | Durée minimale de silence séparant deux zones |
| `min_silence_duration_ms_low_quality` | int | `250` | Silence minimal si audio faible qualité |
| `speech_pad_ms` | int | `200` | Marge ajoutée autour des zones vocales |
| `speech_pad_ms_low_quality` | int | `350` | Marge si audio faible qualité |

**Recommandation actuelle :** VAD actif sur le résumé, désactivé par défaut sur la transcription finale.
La transcription finale utilise déjà les `exclusive_turns` pyannote comme VAD implicite.

#### `workflow.audio_scene`

Analyse acoustique de scène exécutée dans un subprocess CPU isolé (librosa).
Produit les signaux `has_music`, `has_noise`, `speech_ratio` et la distribution H/F
à partir du pitch YIN. Le subprocess se termine avant le chargement GPU.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Active l'analyse de scène (désactivé par défaut, librosa requis) |
| `timeout_s` | int | `120` | Durée maximale allouée au subprocess (secondes) |
| `detect_gender` | bool | `true` | Sous-classification speech → male/female via pitch YIN |
| `thresholds.energy_ratio` | float | `0.03` | Fraction RMS moyenne en-dessous de laquelle une trame est inactive |
| `thresholds.min_segment_s` | float | `0.3` | Segments plus courts ignorés |
| `thresholds.noise_flatness_min` | float | `0.40` | Spectral flatness > seuil → bruit |
| `thresholds.music_flatness_max` | float | `0.12` | Flatness < seuil ET ZCR < zcr_max → musique |
| `thresholds.music_zcr_max` | float | `0.10` | Zero crossing rate maximal pour la classe musique |
| `thresholds.female_pitch_hz` | float | `165.0` | Pitch médian ≥ seuil → voix féminine |

**Redémarrage requis :** non — lu à chaque pipeline via `PipelineService._run_audio_scene_analysis()`.

**Impact :** quand `enabled=true`, le résultat est sauvegardé dans `metadata/audio_scene.json` et transmis à `SourceSeparationDecider` (signaux de scène prioritaires sur le score si `has_music=True`). La distribution H/F est injectée dans `summary/diarization_context.md` et affichée dans l'UI (étape Participants).

#### `workflow.source_separation`

Séparation de sources vocales via Demucs. Ne s'active **jamais automatiquement** :
c'est `SourceSeparationDecider` qui décide sur la base des signaux de
`audio_quality_decision.json` et `audio_scene` (musique détectée → séparation forcée).

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Active la séparation de sources (demucs requis) |
| `backend` | string | `"demucs"` | Backend de séparation (`demucs` uniquement) |
| `model` | string | `"htdemucs"` | Modèle Demucs (`htdemucs` ou `htdemucs_ft` fine-tuned) |
| `device` | string | `"auto"` | Périphérique de calcul (`auto`, `cpu`, `cuda`, `cuda:0`…) |
| `segment_s` | int | `10` | Batch en secondes (compromis mémoire/qualité) |
| `stem` | string | `"vocals"` | Tige extraite (`vocals`, `drums`, `bass`, `other`) |
| `decision.min_score` | int | `3` | Seuil de score pour activer la séparation |
| `decision.min_duration_s` | int | `60` | Audio < seuil → séparation non déclenchée (surcoût injustifié) |

**Redémarrage requis :** non.

**Impact :** si `should_separate()` retourne `True`, la piste vocale extraite (`vocals.wav`) remplace l'audio d'entrée pour le reste du pipeline STT. En cas d'erreur Demucs, l'audio original est conservé sans interruption (dégradation gracieuse).

#### `workflow.speaker_realignment`

Réaligne les locuteurs au niveau mot quand les timestamps `words` Whisper/CTC
sont disponibles et qu'un segment ASR traverse plusieurs tours pyannote.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Active le réalignement locuteur mot-à-mot |
| `min_word_overlap_s` | float | `0.01` | Chevauchement minimal mot/tour pour attribuer un locuteur |
| `punctuation_chars` | string | `".,;:!?)]}»"` | Ponctuations attachées au mot précédent |

### `diarization`

Paramètres de cache pour éviter de relancer pyannote quand l'audio et le modèle
n'ont pas changé.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `cache_enabled` | bool | `true` | Réutilise `speaker_turns.json` si le checkpoint correspond |
| `cache_audio_fingerprint` | bool | `true` | Vérifie taille/mtime/chemin de l'audio avant réutilisation |
| `embedding_cache_enabled` | bool | `true` | Écrit un checkpoint acoustique par locuteur |
| `embedding_clip_seconds` | float | `12.0` | Durée maximale utilisée par locuteur pour le checkpoint |

#### `workflow.execution`

Configuration du worker interne qui exécute les traitements longs hors requête HTTP.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `max_concurrent_jobs` | int | `1` | Nombre maximal de jobs exécutés en parallèle par le worker interne |

**Redémarrage requis :** oui — le worker est instancié au démarrage de l’application.

**Note :** la valeur par défaut `1` est volontaire sur un service GPU partagé. Monter plus haut sans revoir la stratégie VRAM augmentera fortement le risque de contention et d’échec.

#### `workflow.summary_llm`

Configuration de la LLM de résumé.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Active la Phase 2 LLM du résumé |
| `model_id` | string | `""` (obligatoire) | Identifiant du modèle utilisé par `OpenCodeRunner.run_summary()` — doit être défini dans `config.yaml` |
| `api_base` | string | `"http://127.0.0.1:8080/v1"` | URL de base de l'API OpenAI-compatible |
| `timeout_seconds` | int | `120` | Timeout du résumé via opencode |
| `use_chat_api` | bool | absent dans `_DEFAULT_CONFIG` | Ancien paramètre du chemin API direct, non utilisé par le chemin opencode actif |

**Redémarrage requis :** non — lus à chaque appel dans `OpenCodeRunner` et `SummaryGenerator._llm_summarize()`.

**Impact si modifié :**
- `enabled=false` : la Phase 2 est sautée. Le résumé affiche "Résumé de contrôle indisponible (LLM non configurée)."
- `model_id` : utilisé par le chemin actif `OpenCodeRunner.run_summary()` pour choisir le modèle du résumé opencode.
- `timeout_seconds` : défaut 120s (conservateur). En production, monter à 1800+ pour les réunions longues.
- `use_chat_api` : conservé pour compatibilité documentaire/production, mais le code `_llm_summarize()` utilise aujourd'hui directement `/chat/completions` et ce chemin n'est pas appelé par le workflow.

**Note sur la dualité de résumé :** Il existe DEUX chemins de résumé LLM :
1. `_llm_summarize()` dans `summary.py` : appel API direct (requests.post). Actuellement **non appelé** (le commentaire dit "Le résumé LLM est fait dans WorkflowRunner.run_summary Phase 2").
2. `OpenCodeRunner.run_summary()` dans `opencode_runner.py` : lance le CLI opencode avec un prompt fichier. C'est le chemin actif.

Les paramètres `api_base` et `use_chat_api` ne concernent que le chemin 1 (inactif). Les paramètres `enabled`, `model_id` et `timeout_seconds` pilotent le chemin actif `OpenCodeRunner.run_summary()`.

#### `workflow.arbitration_llm`

Configuration du LLM d'arbitrage/correction SRT.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Active l'arbitrage LLM |
| `model_id` | string | `""` (obligatoire) | Identifiant du modèle — doit être défini dans `config.yaml`, `OpenCodeRunner` lève `ValueError` si absent |
| `api_base` | string | `"http://127.0.0.1:8080/v1"` | URL de base de l'API |
| `timeout_seconds` | int | `600` | Timeout de la correction SRT via opencode |
| `opencode_bin` | string | `"opencode"` | Chemin vers le binaire opencode |

**Redémarrage requis :** non.

**État actuel :** la correction SRT utilise `OpenCodeRunner.run_correction()` et lit `model_id`, `timeout_seconds` et `opencode_bin` depuis cette section.

---

### `security`

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `retention_days` | int | `365` | Durée de rétention des jobs terminaux (`completed`, `failed`, `cancelled`) |
| `allow_job_delete` | bool | `true` | Autorise la suppression de jobs (vérifié dans la route `delete_job`) |
| `allowed_upload_extensions` | list[str] | `[".mp3", ".wav", ".m4a", ".mp4", ".flac", ".ogg"]` | Extensions autorisées pour l'upload |

**Redémarrage requis :** non — `retention_days`, `allow_job_delete` et `allowed_upload_extensions` sont vérifiés à l'exécution.

**Impact si modifié :**
- `retention_days` : appliqué par `JobStore.purge_expired_jobs()` lors de l'accès à la page d'accueil. Seuls les jobs anciens en état terminal sont supprimés avec leurs fichiers.
- `allow_job_delete=false` : la route `delete_job` retourne 403. La suppression est bloquée même pour l'admin.
- `allowed_upload_extensions` : extensions vérifiées dans `api_upload`. Les extensions doivent inclure le point (`.mp3`, pas `mp3`).

---

## 6. Variables d'environnement

| Variable | Description | Défaut si absente |
|---|---|---|
| `TRANSCRIA_CONFIG` | Chemin vers le fichier config.yaml | `config.yaml` |
| `TRANSCRIA_SECRET` | Clé secrète Flask (sessions) | `os.urandom(32).hex()` (aléatoire à chaque redémarrage) |
| `TRANSCRIA_PORT` | Port d'écoute (surcharge CLI prioritaire) | Valeur de `config.yaml` ou 7870 |
| `TRANSCRIA_HOST` | Hôte d'écoute | Valeur de `config.yaml` ou `0.0.0.0` |
| `TRANSCRIA_DEBUG` | Mode debug (`"true"` = activé) | Valeur de `config.yaml` ou `false` |
| `HF_TOKEN` | Token HuggingFace pour pyannote | Requis si modèle gated |

**Sécurité :** `TRANSCRIA_SECRET` est aléatoire par défaut, ce qui invalide les sessions existantes à chaque redémarrage du serveur. En production, définir une valeur fixe.

---

## 7. Interface admin de configuration

La route `/admin/config` permet aux administrateurs (`Permission.MANAGE_CONFIG`) d'éditer le YAML de configuration :

1. `GET /admin/config` affiche la configuration effective courante, sérialisée en YAML.
2. `auth.first_admin_password` est remplacé par `********` à l'affichage.
3. `POST /admin/config` valide le YAML avec `yaml.safe_load`.
4. Le YAML est sauvegardé via `save_config()`.
5. La configuration effective est rechargée par `load_config()` puis injectée dans le singleton avec `set_config()`.

Limites :
- `server.host`, `server.port`, `server.debug` et `storage.database_url` nécessitent un redémarrage complet pour affecter le process Flask ou SQLAlchemy déjà initialisé.
- Les objets déjà construits avec une ancienne config ne sont pas mis à jour automatiquement.

---

## 8. Scripts externes (configurables via config + env)

`VRAMManager` lit ces valeurs dans `config.yaml` avec fallback :

| Paramètre | Défaut | Description |
|---|---|---|
| `services.arbitrage_script` | `./scripts/launch_arbitrage.sh` | Script bash de lancement de la LLM d'arbitrage |
| `services.stop_script` | `./scripts/stop_arbitrage_llm.sh` | Script bash d'arrêt de la LLM d'arbitrage |
| `services.arbitrage_llm_port` | `8080` | Port du serveur LLM d'arbitrage |
| `services.qwen_port` | `8080` | Ancien nom compatible, à ne plus utiliser dans les nouvelles configs |
| `services.llm_cleanup_ports` | `[8000]` | Ports de backends LLM concurrents à libérer avant lancement |
| `services.vllm_port` | `8000` | Ancien nom compatible, converti en `llm_cleanup_ports` |
| `gpu.cohere_vram_mb` | `6000` | VRAM estimée Cohere |
| `gpu.pyannote_vram_mb` | `2000` | VRAM estimée pyannote |
| `gpu.llm_vram_mb` | `60000` | VRAM estimée LLM |
| `gpu.min_free_vram_mb` | `4000` | VRAM minimale libre |

Overrides environnement :
- `TRANSCRIA_ARBITRAGE_SCRIPT`
- `TRANSCRIA_STOP_SCRIPT`

Note d'exploitation :
- Le script livré `services.arbitrage_script` lance actuellement `llama.cpp` (`llama-server`) avec le modèle local configuré sur cette machine.
- `services.llm_cleanup_ports` est volontairement générique : il peut contenir des ports vLLM, SGLang, llama.cpp, ik_llama.cpp ou tout autre serveur OpenAI-compatible concurrent.
- La clé `qwen_port` reste acceptée en lecture par `_normalize_config` (alias de `arbitrage_llm_port`). Les méthodes `launch_qwen_35b()` et `stop_qwen_35b()` ont été supprimées — utiliser `launch_arbitrage_llm()` et `stop_arbitrage_llm()`.
- Le nombre de GPUs et la VRAM réellement consommée ne sont pas figés : ils dépendent du script (ex: `--tensor-split`), du modèle GGUF, du contexte et de la machine.

---

## 9. Qualité SRT

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `quality.asr_noise_markers` | list[string] | liste courte configurable | Expressions courtes à traiter comme bruit ASR probable quand elles apparaissent dans un segment très court |

Ces marqueurs ne corrigent pas le SRT automatiquement. Ils alimentent seulement le rapport qualité pour orienter la relecture humaine vers les segments courts suspects.

---

## 10. Fichiers de prompts opencode (configs/prompts/)

| Fichier | Utilisé par | Description |
|---|---|---|
| `summary_prompt.txt` | `OpenCodeRunner.run_summary()` | Prompt système pour le résumé structuré (133 lignes) |
| `correction_prompt.txt` | `OpenCodeRunner.run_correction()` | Prompt système pour la correction SRT (224 lignes) |

Les chemins sont résolus relativement à `transcria/gpu/opencode_runner.py` (remonte de 2 niveaux).

---

## 10. Matrice de redémarrage

| Paramètre | Redémarrage requis ? | Lu dynamiquement ? |
|---|:---:|:---:|
| `server.host` | Oui | Non |
| `server.port` | Oui | Non |
| `server.debug` | Oui | Non |
| `storage.jobs_dir` | Non | Oui (JobFilesystem) |
| `storage.database_url` | Oui | Non |
| `auth.enabled` | Non (normalisé à true) | Oui (load/save) |
| `auth.first_admin_*` | Non (une seule fois) | Non |
| `services.dashboard_llm_url` | Non (VRAMManager) | Oui (instancié) |
| `services.srt_editor_easy_url` | Non | Oui (template) |
| `models.cohere_model_path` | Non | Oui (CohereTranscriber) |
| `models.pyannote_model` | Non | Oui (DiarizerService) |
| `models.default_stt_model` | Non | Oui (chargé à la création des services STT) |
| `models.fallback_stt_model` | Non | Oui |
| `workflow.enable_*` | Non | Oui |
| `workflow.vad.*` | Non | Oui |
| `workflow.audio_scene.*` | Non | Oui (PipelineService) |
| `workflow.source_separation.*` | Non | Oui (PipelineService) |
| `workflow.summary_llm.*` | Non | Oui (SummaryGenerator, OpenCodeRunner) |
| `workflow.arbitration_llm.*` | Non | Oui |
| `quality.asr_noise_markers` | Non | Oui |
| `security.retention_days` | Non | Oui (purge à l'accueil) |
| `security.allow_job_delete` | Non | Oui (route) |
| `security.allowed_upload_extensions` | Non | Oui (route) |

**Problème architecturel :** `get_config()` retourne un singleton. Si `set_config()` est appelé pour mettre à jour une valeur, les routes qui rappellent `get_config()` voient le changement, mais les objets déjà construits avec une ancienne config ne sont pas mis à jour automatiquement.

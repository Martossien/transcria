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
| `workflow.arbitration_llm.timeout_seconds` | 7200 | typ. 7200 (défaut code : 600) |
| `workflow.summary_llm.use_chat_api` | absent | `true` |

La clé `qwen_port` reste lue pour compatibilité avec les anciennes installations (alias de `arbitrage_llm_port`). Les nouvelles configurations doivent utiliser `arbitrage_llm_port`, `stop_arbitrage_llm.sh` et `llm_cleanup_ports`. Les méthodes Python `launch_qwen_35b()` et `stop_qwen_35b()` ont été supprimées — utiliser `launch_arbitrage_llm()` et `stop_arbitrage_llm()`.

La détection d'une LLM d'arbitrage déjà active repose sur l'API OpenAI-compatible (`/v1/models` puis `/v1/completions`) et non sur `lsof` seul. Un serveur sain avec le bon `arbitrage_api_model_id` est réutilisé directement (CAS A), même si le PID n'est pas détectable par les outils système locaux.

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
| `database_url` | string | `"sqlite:///transcrIA.db"` | URL SQLAlchemy. **PostgreSQL recommandé en prod** (`postgresql+psycopg://…`, requis pour la concurrence Phase B) ; SQLite = repli mono-process dev/tests. La variable d'env `TRANSCRIA_DATABASE_URL` (prioritaire) garde le mot de passe hors config versionnée |

**Redémarrage requis :** oui pour `database_url`. `jobs_dir` est relu par `JobFilesystem` à chaque opération (pas de cache).

**Impact si modifié :**
- `jobs_dir` : les jobs existants ne sont PAS déplacés. Si le chemin change, les anciens jobs sont "perdus" (fichiers toujours sur disque mais base orpheline de ces fichiers).
- `database_url` : le schéma est géré par **Alembic** (`alembic upgrade head`, lancé par `start.sh`) ; `db.create_all()` ne sert qu'au bootstrap dev/tests. Changer d'URL nécessite de migrer les données (`scripts/migrate_sqlite_to_postgres.py`). Voir `docs/INSTALL.md` §7.

---

### `runtime`

Rôle du process pour la montée en charge (Phase B). Voir [`CONCURRENCE_ET_CHARGE_PHASE_B.md`](CONCURRENCE_ET_CHARGE_PHASE_B.md).

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `role` | string | `"all"` | `all` (tout-en-un mono-process) \| `web` (tier HTTP sans état, `gunicorn -w N`, n'exécute pas la file) \| `scheduler` (orchestrateur **unique** qui draine la file et exécute les jobs). Surchargé par `TRANSCRIA_ROLE` |

**Invariant :** en distribué, lancer **un seul** process `scheduler` + N process `web`. Un verrou consultatif PostgreSQL (`scheduler_lock.py`) refuse un second ordonnanceur. PostgreSQL est requis ; en SQLite on reste forcément en `all`.

---

### `auth`

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Toujours normalisé à `true` : le mode sans authentification n'est pas supporté |
| `first_admin_username` | string | `"admin"` | Login du premier admin créé si la base est vide |
| `first_admin_password` | string | `"CHANGE-ME"` | Mot de passe du premier admin |

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
- `dashboard_llm_url` : utilisé par `VRAMManager` pour interroger l'API GPU (`/api/v1/gpus`). Si le dashboard est indisponible, `VRAMManager` bascule sur `torch.cuda.mem_get_info()`.
- `srt_editor_easy_url` : utilisé pour le bouton "Ouvrir dans SRT Editor" et l'API `push-to-editor`. Si l'URL est incorrecte, le bouton apparaît mais la redirection échoue.

---

### `models`

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `stt_backend` | string | `"cohere"` | Backend STT (`cohere`, `cohere_tf5`, `whisper`, `granite` ou `parakeet`) |
| `diarization_backend` | string | `"pyannote"` | Backend de diarisation (`pyannote` ou `sortformer`) — sélectionné par `create_diarizer()` dans `diarizer_factory.py` |
| `default_stt_model` | string | `"cohere-transcribe-03-2026"` | Modèle STT par défaut |
| `fallback_stt_model` | string | `"large-v3"` | Modèle fallback |
| `cohere_model_path` | string | `"./models/cohere-asr/cohere-transcribe-03-2026"` | Chemin vers le modèle Cohere ASR local |
| `pyannote_model` | string | `"pyannote/speaker-diarization-community-1"` | Nom du modèle pyannote HuggingFace |

**Redémarrage requis :** non — les chemins sont lus à chaque transcription/diarization.

**Impact si modifié :**
- `cohere_model_path` : si le chemin est invalide, `CohereTranscriber.load()` échoue avec un avertissement. Le chemin est résolu en absolu si c'est un répertoire local (`os.path.abspath`). Si le chemin commence par `CohereLabs/` ou `cohere/`, HuggingFace download est utilisé.
- `pyannote_model` : doit être un modèle HuggingFace valide. Nécessite d'accepter les conditions sur huggingface.co et configurer `HF_TOKEN` pour les modèles gated.
- `stt_backend` pilote la sélection du backend via `TranscriberFactory`.
- `diarization_backend` pilote la sélection via `create_diarizer()`. Un backend inconnu déclenche un warning et bascule sur `pyannote`. La VRAM réservée est lue via `get_diarizer_vram_mb(backend, config)`.

### `cohere`

Paramètres optionnels du backend Cohere ASR principal. Cohere reste le backend
production recommandé à ce stade (`models.stt_backend=cohere`). Ces paramètres
ne sont lus que si une section `[cohere]` existe dans `config.yaml`. En l'absence
de cette section, les valeurs par défaut sont utilisées par `CohereTranscriber`.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `chunk_length_s` | int | `30` | Durée des chunks ASR en secondes |
| `max_new_tokens` | int | `448` | Nombre maximal de tokens générés par chunk |
| `punctuation` | bool | `true` | Demande au modèle Cohere de générer la ponctuation via son prompt natif (`false` à réserver aux benchmarks qualité/WER) |
| `repetition_penalty` | float | `1.2` | Pénalité de répétition pour Cohere |
| `no_repeat_ngram_size` | int | `4` | Taille des n-grams bloqués |
| `collapse_repetition_loops` | bool | `true` | Activer la détection/réduction des boucles répétitives dans la transcription Cohere |
| `repetition_loop_min_repeats` | int | `4` | Nombre minimal de répétitions pour détecter une boucle |
| `repetition_loop_max_phrase_words` | int | `10` | Nombre maximal de mots dans une phrase répétée |
| `repetition_loop_keep_repeats` | int | `2` | Nombre de répétitions à conserver après réduction |
| `lexicon_biasing.enabled` | bool | `false` | Active le biasing contextuel expérimental Cohere depuis les termes validés du lexique de session |
| `lexicon_biasing.priorities` | list[str] | `["critique", "importante", "normale"]` | Priorités injectables dans le Trie Cohere |
| `lexicon_biasing.max_terms` | int | `300` | Nombre maximum de termes cibles retenus |
| `lexicon_biasing.boost` | float | `0.2` | Bonus de logit appliqué aux tokens qui prolongent un terme déjà amorcé |
| `lexicon_biasing.start_boost` | float | `0.05` | Bonus léger appliqué aux premiers tokens possibles des termes validés |
| `lexicon_biasing.max_prefix_tokens` | int | `20` | Profondeur maximale de recherche du préfixe dans chaque beam |

**Redémarrage requis :** oui — le modèle est chargé en mémoire GPU.

### `cohere_tf5`

Backend expérimental opt-in (`models.stt_backend=cohere_tf5`) utilisant la classe
native Transformers 5 `CohereAsrForConditionalGeneration`. Il doit rester isolé
tant que Transformers 5 tire des versions incompatibles avec NeMo/datasets/lightning :
installer la pile TF5 dans un répertoire dédié (`pip --target`) et pointer
`cohere_tf5.tf5_site` vers ce répertoire. Cohere classique reste le défaut.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Marque documentaire ; l'activation réelle se fait via `models.stt_backend=cohere_tf5` |
| `tf5_site` | string | `"/tmp/transcria_tf54_site"` | Répertoire `site-packages` isolé contenant Transformers 5 |
| `model_path` | string | `"CohereLabs/cohere-transcribe-03-2026"` | Modèle Cohere ASR natif TF5 |
| `model_revision` | string | `""` | Révision HF optionnelle |
| `timeout_s` | int | `7200` | Timeout du worker subprocess TF5 |
| `chunk_length_s` | int | `30` | Durée des chunks ASR |
| `max_new_tokens` | int | `448` | Nombre maximal de tokens générés |
| `punctuation` | bool | `true` | Demande la ponctuation via le processor TF5 |
| `batch_size` | int | `96` | Batch de chunks internes quand un fichier complet est transcrit |
| `repetition_penalty` | float | `1.2` | Pénalité de répétition |
| `no_repeat_ngram_size` | int | `4` | Taille des n-grams bloqués |
| `collapse_repetition_loops` | bool | `true` | Réduction des boucles répétitives post-ASR |
| `repetition_loop_min_repeats` | int | `4` | Répétitions minimales pour détecter une boucle |
| `repetition_loop_max_phrase_words` | int | `10` | Taille maximale d'une phrase répétée |
| `repetition_loop_keep_repeats` | int | `2` | Répétitions conservées après réduction |

**Usage recommandé :** bench et expérimentation contrôlée avec pyannote activé.
Ne pas en faire le défaut avant d'ajouter des garde-fous de couverture et fallback
Whisper sur sous-transcription.

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
| `vad_filter` | bool | `false` | VAD interne faster-whisper, désactivé par défaut (trop agressif pour le français, voir `docs/VAD_OR_NOT.md`) |
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
| `lexicon_hotwords.enabled` | bool | `false` | Injecte les termes du lexique de session dans les hotwords Whisper quand le backend effectif est Whisper |
| `lexicon_hotwords.priorities` | list[str] | `["critique", "importante"]` | Priorités de lexique injectables |
| `lexicon_hotwords.max_terms` | int | `50` | Nombre maximum de termes injectés |
| `lexicon_hotwords.max_chars` | int | `900` | Longueur maximale de la chaîne hotwords construite |
| `lexicon_hotwords.max_tokens` | int | `200` | Budget de tokens Whisper pour les hotwords construits depuis le lexique |
| `lexicon_hotwords.tokenizer_model` | string | `"openai/whisper-large-v3"` | Tokenizer local utilisé pour compter les tokens hotwords ; fallback approximatif si indisponible |
| `lexicon_hotwords.prefix` | string | `"Termes importants :"` | Préfixe utilisé si aucun hotword statique n'est configuré |
| `collapse_repetition_loops` | bool | `true` | Réduit les boucles textuelles répétées après ASR |
| `repetition_loop_min_repeats` | int | `4` | Nombre minimum de répétitions consécutives suspectes |
| `repetition_loop_max_phrase_words` | int | `10` | Taille maximale d'une phrase répétée détectée |
| `repetition_loop_keep_repeats` | int | `2` | Occurrences conservées après réduction d'une boucle |

### `granite`

Backend STT expérimental IBM Granite Speech 4.1 2B. Il reste désactivé par défaut
car `models.stt_backend` vaut `cohere`; il peut être activé explicitement pour des
tests ou campagnes ciblées avec `models.stt_backend=granite`.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Marque documentaire/expérimentale ; le choix effectif reste `models.stt_backend` |
| `model_id` | string | `"./models/granite-speech-4.1-2b"` | Chemin local ou identifiant HuggingFace du modèle Granite normal |
| `torch_dtype` | string | `"bfloat16"` | Type torch (`bfloat16`, `float16`, `float32`) |
| `chunk_length_s` | int | `300` | Durée maximale d'un chunk Granite |
| `max_new_tokens` | int | `2000` | Plafond absolu de génération par chunk |
| `max_new_tokens_per_second` | float/null | `8.0` | Borne dynamique du budget selon la durée du chunk ; `null` désactive le scaling |
| `min_new_tokens` | int | `64` | Budget minimal conservé quand le chunk est court |
| `prompt_mode` | string | `"asr_punctuated"` | Prompt utilisé (`asr_raw`, `asr_punctuated`, `keywords`) |
| `prompt_asr_raw` | string | prompt IBM | Prompt brut sans ponctuation forcée |
| `prompt_asr_punctuated` | string | prompt IBM | Prompt de transcription avec ponctuation/capitalisation |
| `prompt_keywords` | string | prompt IBM | Prompt avec `{keywords}` pour tests de biasing Granite |
| `keywords` | list/string | `[]` | Mots-clés passés si `prompt_mode=keywords` |
| `fix_mistral_regex` | bool | `true` | Passe le correctif tokenizer Granite/Mistral à `AutoProcessor` quand supporté |
| `collapse_repetition_loops` | bool | `true` | Réduit les boucles répétitives après génération |
| `repetition_loop_min_repeats` | int | `4` | Répétitions minimales pour détecter une boucle |
| `repetition_loop_max_phrase_words` | int | `10` | Longueur maximale d'une phrase répétée |
| `repetition_loop_keep_repeats` | int | `2` | Répétitions conservées après réduction |

**Redémarrage requis :** oui — le modèle est chargé en VRAM.

**Impact si modifié :**
- `model_id` local évite tout accès réseau au runtime. Le modèle téléchargé dans
  `models/granite-speech-4.1-2b/` est ignoré par git.
- Si la version de `transformers` ne supporte pas `fix_mistral_regex`, le backend
  réessaie sans ce paramètre et logue un warning explicite.
- `metadata/granite.json` trace le modèle, le device, le prompt, le fix appliqué,
  les durées et le nombre de chunks.

### `parakeet`

Backend STT expérimental NVIDIA Parakeet TDT 0.6B v3 via NeMo. Désactivé par
défaut : Cohere reste le backend production normal. Activé via
`models.stt_backend=parakeet`.

Limites connues : pas de word boosting ; l'ITN NeMo peut écrire les nombres
en lettres ; la détection automatique de langue peut basculer sur l'anglais
avec des accents ou hésitations. Documenté dans `docs/PARAKEET_STT_INTEGRATION.md`.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Activation du backend |
| `model_id` | string | `"nvidia/parakeet-tdt-0.6b-v3"` | Identifiant HuggingFace |
| `use_local_attention` | bool | `true` | Attention locale pour support audio long (3h au lieu de 24 min) |
| `att_context_size` | list | `[256, 256]` | Taille du contexte d'attention (gauche, droite) |
| `decoding_strategy` | string | `"greedy_batch"` | Stratégie de décodage (`beam` cassé pour timestamps dans NeMo 2.7.3) |
| `decoding_beam_size` | int | `2` | Taille du beam (si `decoding_strategy=beam`) |
| `max_chunk_duration_s` | int | `1200` | Durée max d'un chunk avant pré-découpage (20 min) |
| `collapse_repetition_loops` | bool | `true` | Anti-hallucination (même mécanisme que Cohere/Granite) |

VRAM : `gpu.parakeet_vram_mb` (défaut 8000 Mo). Dépendance : `nemo_toolkit[asr]`.
Fichier : `metadata/parakeet.json`.

### `sortformer`

Backend de diarisation NVIDIA Sortformer 4 locuteurs via NeMo. Activé via
`models.diarization_backend=sortformer`. Désactivé par défaut (pyannote reste
le backend de production). Nécessite `nemo_toolkit[asr]`.

Contrairement à pyannote, Sortformer retourne des segments exclusifs par
construction (pas de chevauchements), avec un maximum de 4 locuteurs simultanés.
Les IDs `speaker_0…speaker_N` produits par NeMo sont normalisés en `SPEAKER_00…SPEAKER_NN`
pour rester compatibles avec le pipeline TranscrIA.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `model_id` | string | `"nvidia/diar_streaming_sortformer_4spk-v2.1"` | Identifiant HuggingFace du modèle NeMo |
| `vram_mb` | int | `3500` | VRAM réservée (aussi lisible via `gpu.sortformer_vram_mb`) |

**Redémarrage requis :** oui — le modèle est chargé en VRAM à l'exécution.

**Impact si modifié :**
- `model_id` : doit être un modèle NeMo `SortformerEncLabelModel` valide. Le modèle est mis en cache dans `~/.cache/huggingface/hub/`. HF_HUB_OFFLINE=1 est actif au runtime — pré-télécharger le modèle avant le premier usage.
- Fallback gracieux : si `nemo_toolkit` n'est pas installé, `SortformerDiarizer.available` retourne `False` et `diarize()` retourne `{"available": False, ...}` sans exception.

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

Contrôle un éventuel forçage du backend STT. Par défaut, Cohere reste le backend
principal en mode `fast` comme en mode `quality`. Whisper large-v3 peut être forcé
explicitement pour des tests, des fallbacks ou des campagnes ciblées.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `force_stt_backend` | string/null | `null` | Backend forcé quand une règle explicite s'applique |
| `enabled_for_modes` | list[string] | `[]` | Modes de traitement qui forcent le backend configuré |
| `force_on_degraded_summary` | bool | `false` | Force le backend configuré si `summary/summary.json` signale un niveau dégradé |
| `degraded_summary_levels` | list[string] | `["degrade"]` | Niveaux de diagnostic considérés comme dégradés |

#### `workflow.audio_quality`

Agrège les signaux ffprobe, les diagnostics du résumé rapide et, si disponible,
l'analyse de scène audio pour produire un diagnostic qualité. Le forçage backend
n'est appliqué que si `workflow.quality_transcription.force_on_degraded_summary`
est activé et qu'un backend cible est configuré. Par défaut, les signaux de scène
sont enregistrés pour audit mais ne modifient pas le score.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `force_quality_backend` | bool | `true` | Signal de forçage qualité exploité seulement si `quality_transcription` l'autorise |
| `degraded_levels` | list[string] | `["degrade"]` | Niveaux de résumé considérés dégradés |
| `suspect_levels` | list[string] | `["suspect"]` | Niveaux de résumé suspects, pondérés plus faiblement |
| `min_bit_rate` | number/null | `64000` | Bitrate minimal avant signal qualité faible |
| `min_sample_rate_hz` | number/null | `16000` | Fréquence minimale avant signal qualité faible |
| `max_non_latin_segments` | number/null | `2` | Nombre maximal de segments non latins toléré |
| `max_short_segment_ratio` | number/null | `0.2` | Ratio maximal de segments courts suspects |
| `min_speech_ratio` | number/null | `0.35` | Ratio VAD minimal avant suspicion de VAD trop agressif |
| `max_speech_ratio` | number/null | `0.95` | Ratio VAD maximal avant suspicion de VAD peu sélectif |
| `scene_affects_quality_score` | bool | `false` | Si `true`, les signaux de scène contribuent au score qualité |
| `max_scene_music_ratio` | number/null | `0.15` | Ratio musique maximal avant signal `scene_musique_importante` |
| `max_scene_noise_ratio` | number/null | `0.20` | Ratio bruit maximal avant signal `scene_bruit_important` |
| `max_scene_no_energy_ratio` | number/null | `0.30` | Ratio sans énergie maximal avant signal `scene_inactivite_importante` |
| `min_scene_speech_ratio` | number/null | `0.55` | Ratio parole minimal avant signal `scene_parole_faible` |
| `max_scene_problem_segments` | number/null | `3` | Nombre maximal de zones problématiques longues toléré |

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
| `auto_enable_final_on_degraded` | bool | `false` | Active automatiquement le VAD final si la décision qualité est dans `auto_enable_final_levels` (désactivé par défaut, voir `docs/VAD_OR_NOT.md`) |
| `auto_enable_final_levels` | list | `["degrade"]` | Niveaux de qualité qui déclenchent le VAD final automatique |
| `threshold_final_degraded` | float | `0.6` | Seuil VAD utilisé quand le VAD final est activé automatiquement sur audio dégradé |
| `adaptive` | bool | `true` | Ajuste les seuils VAD selon `metadata/audio_quality_decision.json` |
| `threshold` | float | `0.5` | Seuil Silero |
| `threshold_low_quality` | float | `0.35` | Seuil appliqué si audio dégradé/faible qualité |
| `threshold_high_noise` | float | `0.6` | Seuil appliqué si VAD peu sélectif |
| `min_speech_duration_ms` | int | `250` | Durée minimale de parole détectée |
| `min_silence_duration_ms` | int | `400` | Durée minimale de silence séparant deux zones |
| `min_silence_duration_ms_low_quality` | int | `250` | Silence minimal si audio faible qualité |
| `speech_pad_ms` | int | `200` | Marge ajoutée autour des zones vocales |
| `speech_pad_ms_low_quality` | int | `350` | Marge si audio faible qualité |
| `hysteresis_enabled` | bool | `false` | Activer la binarisation par hystérésis des scores VAD |
| `onset` | float | `0.5` | Seuil d'apparition pour l'hystérésis (onset) |
| `offset` | float | `0.35` | Seuil de disparition pour l'hystérésis (offset) |

**Recommandation actuelle :** VAD actif sur le résumé, désactivé par défaut sur la transcription finale.
La transcription finale utilise déjà les `exclusive_turns` pyannote comme VAD implicite.
Sur audio dégradé (qualité `degrade`), le VAD final est activé automatiquement avec un seuil
de 0.6, ce qui réduit le temps pipeline sans perte de contenu mesurable (validé sur CSE 1h40).

#### `workflow.audio_scene`

Analyse acoustique de scène exécutée dans un subprocess CPU isolé (librosa).
Produit les signaux `has_music`, `has_noise`, `speech_ratio`, les ratios non vocaux,
les segments horodatés et la distribution H/F à partir du pitch YIN. Le subprocess
se termine avant le chargement GPU.

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
| `thresholds.music_suppress_bandwidth_hz` | float | `3000.0` | Si la bande passante médiane (rolloff 95 %) < seuil, la classe `music` est neutralisée (parole bande étroite faussement classée musique). `0` = garde désactivée |
| `thresholds.female_pitch_hz` | float | `165.0` | Pitch médian ≥ seuil → voix féminine |
| `thresholds.problem_segment_min_s` | float | `2.0` | Durée minimale d'une zone non vocale exposée dans `problem_segments` |

**Redémarrage requis :** non — lu à chaque pipeline via `PipelineService._run_audio_scene_analysis()`.

**Impact :** quand `enabled=true`, le résultat est sauvegardé dans `metadata/audio_scene.json` et transmis à `SourceSeparationDecider` avec des seuils explicites de ratio/durée. La distribution H/F est injectée dans `summary/diarization_context.md` et affichée dans l'UI (étape Participants).

#### `workflow.audio_scene_filter`

Filtrage optionnel pré-STT basé sur `metadata/audio_scene.json`. Il ne coupe pas
l'audio : il met en silence les zones ciblées pour préserver la durée totale et
les timestamps du SRT. Désactivé par défaut.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Active le filtrage par analyse de scène |
| `enabled_for_modes` | list[string] | `["quality"]` | Modes où le filtre peut s'appliquer (`fast`, `quality`) |
| `target_labels` | list[string] | `["music", "noise"]` | Labels de `problem_segments` à mettre en silence (`music`, `noise`, `noEnergy`) |
| `min_segment_s` | float | `2.0` | Durée minimale après marge pour filtrer un intervalle |
| `min_total_muted_s` | float | `2.0` | Durée totale minimale filtrée pour lancer ffmpeg |
| `edge_keep_s` | float | `0.15` | Marge conservée au début/à la fin de chaque zone |
| `max_intervals` | int | `100` | Nombre maximal d'intervalles filtrés |
| `timeout_s` | int | `300` | Timeout ffmpeg en secondes |

**Impact :** si le filtre s'applique, `input/scene_filtered.wav` remplace l'audio transmis au STT et `metadata/audio_scene_filter.json` documente les intervalles. En cas d'erreur ffmpeg, l'audio original est conservé.

#### `workflow.audio_preflight`

Pré-diagnostic acoustique exécuté avant le pipeline STT. Analyse RMS, SNR estimé,
bande passante et clipping pour produire des flags (`audio_faible`, `audio_tres_faible`,
`snr_faible`, `bande_passante_faible`, `clipping`, `silence`) utilisés par les étapes
ultérieures (normalisation auto, débruitage, VAD adaptatif). Le résultat est sauvegardé
dans `metadata/audio_preflight.json`.

Le preflight peut être enrichi par trois qualifications optionnelles (sous-sections
`squim`, `dnsmos`, `acoustic`) qui ajoutent des scores perceptifs/prédictifs et une
`difficulty_map` par fenêtre. Détail de la conception : `docs/STT_ADAPTATIF_ET_HYBRIDE.md`.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Activer l'analyse pré-diagnostic acoustique avant le pipeline |
| `frame_ms` | float | `30` | Durée en ms des trames d'analyse RMS |
| `low_rms_threshold` | float | `0.02` | Seuil RMS en dessous duquel l'audio est signalé `audio_faible` |
| `very_low_rms_threshold` | float | `0.008` | Seuil RMS en dessous duquel l'audio est signalé `audio_tres_faible` |
| `silence_rms_threshold` | float | `0.003` | Seuil RMS pour le flag `silence` |
| `low_snr_db_threshold` | float | `6.0` | SNR estimé en dessous duquel l'audio est signalé `snr_faible` |
| `narrowband_hz_threshold` | float | `3800.0` | Bande passante en Hz en dessous de laquelle l'audio est signalé `bande_passante_faible` |
| `clipping_threshold` | float | `0.98` | Seuil d'amplitude pour la détection de clipping |
| `clipping_ratio_threshold` | float | `0.001` | Proportion d'échantillons clipping pour signaler le flag `clipping` |

**Redémarrage requis :** non — lu à chaque pipeline via `PipelineService._run_audio_preflight()`.

##### `workflow.audio_preflight.squim`

Qualification SQUIM non-intrusive (STOI / PESQ / SI-SDR, modèle `SquimObjective` de
torchaudio, CC-BY-4.0). Produit `squim_global` et, en lazy, une `difficulty_map` par
fenêtre. Le **score global est borné** : il échantillonne quelques fenêtres réparties
(`probes × window_s`, par défaut 5 × 10 s) puis moyenne — il ne passe jamais le fichier
entier au modèle (sinon allocation démesurée → OOM sur audio long).

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Activer la qualification SQUIM (score global + difficulty_map lazy) |
| `segment_s` | float | `5.0` | Durée des fenêtres de la `difficulty_map` |
| `hop_s` | float | `2.5` | Pas entre fenêtres **sur GPU** (pleine résolution) |
| `hop_s_cpu` | float | `5.0` | Pas entre fenêtres **en repli CPU** : élargi (≈÷2 fenêtres) pour privilégier la vitesse, le scoring par fenêtre CPU étant l'étape la plus coûteuse du preflight |
| `device` | string | `"auto"` | `auto` → **GPU le plus libre** ayant ≥ `vram_mb` de VRAM, sinon CPU. Index explicite (`cuda:2`) respecté. Repli CPU automatique si un lot OOM (collant : plus de tentative CUDA ensuite) |
| `vram_mb` | int | `5000` | VRAM requise pour placer SQUIM sur un GPU (≈4,8 Go observés + marge). Sert au choix du GPU le plus libre ; aucun éligible → CPU |
| `stoi_threshold` | float | `0.70` | STOI sous ce seuil → flag `squim_stoi_faible` |
| `pesq_threshold` | float | `2.5` | PESQ sous ce seuil → flag `squim_pesq_faible` |
| `sisdr_threshold` | float | `5.0` | SI-SDR (dB) sous ce seuil → flag `squim_sisdr_faible` |
| `difficulty_map_always` | bool | `false` | `true` calcule la `difficulty_map` même si l'audio est « ok » (utile pour le bench) |

**Choix du GPU (multi-GPU) :** `device: auto` sélectionne, en lecture seule
(`torch.cuda.mem_get_info`), le GPU **le plus libre** ayant ≥ `vram_mb`. Sur une machine
dont le GPU 0 est occupé par le LLM d'arbitrage, SQUIM est ainsi placé sur un GPU libre
(p. ex. `cuda:7`) **sans jamais évincer le LLM**. Si aucun GPU n'a la place (frontale sans
GPU, ou tous occupés), repli CPU avec frise grossie (`hop_s_cpu`).

**Note concurrence :** le modèle SQUIM est un singleton torch partagé ; ses inférences
sont sérialisées par un verrou interne (sûr quand plusieurs jobs lancent le preflight en
parallèle, hors sérialisation de l'allocateur GPU).

**Redémarrage requis :** non — lu à chaque pipeline.

##### `workflow.audio_preflight.dnsmos`

Qualification perceptive DNSMOS P.835 (SIG / BAK / OVRL, MOS 1-5, modèle ONNX embarqué
CC-BY-4.0). **Indépendante de SQUIM** : calculée en premier sur des sondes bornées (≤ 5
fenêtres de 9 s), elle reste donc disponible même si SQUIM échoue. Inférence onnxruntime
sur CPU (thread-safe).

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Activer la qualification DNSMOS |
| `ovrl_threshold` | float | `2.5` | OVRL sous ce seuil → flag `dnsmos_ovrl_faible` (peut déclencher la difficulty_map) |
| `sig_bak_margin` | float | `0.0` | SIG < BAK − marge → signal `sig_lt_bak` (parole intrinsèquement dégradée) |

**Redémarrage requis :** non — lu à chaque pipeline.

##### `workflow.audio_preflight.acoustic`

Métriques acoustiques par fenêtre (RT60, C50, SNR, suspicion de codec) calculées en
numpy/scipy (CPU), injectées comme signaux dans la `difficulty_map`.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Activer les métriques acoustiques par fenêtre |
| `rt60_threshold` | float | `0.6` | RT60 (s) au-delà duquel le signal `rt60_eleve` est posé (réverbération longue) |
| `snr_threshold` | float | `6.0` | SNR par fenêtre (dB) sous lequel `snr_faible` est posé |
| `c50_threshold` | float | `-5.0` | C50 (dB) sous lequel `c50_faible` est posé (clarté faible) |

**Redémarrage requis :** non — lu à chaque pipeline.

#### `workflow.audio_denoise`

Débruitage audio optionnel via ffmpeg (filtre `afftdn`). Désactivé par défaut car
expérimental. Le débruitage ne s'applique que si les flags preflight correspondent
aux `trigger_flags` configurés (ou si `force=true`). Produit `input/denoised.wav` et
`metadata/audio_denoise.json` avec `preserve_timeline=true`.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Activer le débruitage ffmpeg (expérimental) |
| `enabled_for_modes` | list | `["quality"]` | Modes dans lesquels le débruitage est activé |
| `backend` | string | `"ffmpeg_afftdn"` | Backend de débruitage (actuellement seul `ffmpeg_afftdn` supporté) |
| `force` | bool | `false` | Forcer le débruitage même si les flags preflight ne correspondent pas |
| `trigger_flags` | list | `["snr_faible"]` | Flags preflight qui déclenchent le débruitage |
| `noise_reduction_db` | float | `12.0` | Réduction de bruit en dB pour le filtre afftdn |
| `noise_floor_db` | float | `-25.0` | Niveau de bruit plancher en dB |
| `timeout_s` | float | `300` | Timeout en secondes pour l'opération ffmpeg |

**Redémarrage requis :** non — lu à chaque pipeline via `PipelineService._run_audio_denoise()`.

**Impact :** si le débruitage s'applique, `input/denoised.wav` remplace l'audio transmis au STT et `metadata/audio_denoise.json` documente l'opération. En cas d'erreur ffmpeg, l'audio original est conservé.

#### `workflow.audio_normalization`

Normalisation audio légère optionnelle avant STT. Elle utilise ffmpeg, conserve
la durée du média et reste désactivée par défaut tant que les gains ne sont pas
validés sur corpus interne.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Active la normalisation pré-STT |
| `enabled_for_modes` | list[string] | `["quality"]` | Modes où la normalisation peut s'appliquer |
| `loudnorm_enabled` | bool | `true` | Active le filtre loudness `loudnorm` |
| `target_i` | float | `-23.0` | Loudness intégré cible |
| `true_peak` | float | `-2.0` | True peak cible |
| `lra` | float | `11.0` | Loudness range cible |
| `highpass_hz` | float/null | `null` | Fréquence du high-pass optionnel ; `null` le désactive |
| `auto_loudnorm_rms_threshold` | float | `0.02` | Seuil RMS en dessous duquel `loudnorm` est forcé automatiquement (même si `enabled=false`) |
| `timeout_s` | int | `300` | Timeout ffmpeg en secondes |

##### `workflow.audio_normalization.weak_voice`

Traitement des audios très faibles (flags `audio_faible`/`audio_tres_faible` du preflight).
Applique un gain puis loudnorm pour remonter le volume sans écrêter. Ce traitement
s'active automatiquement même si `workflow.audio_normalization.enabled=false`.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Activer le traitement weak_voice pour les audios très faibles |
| `target_rms` | float | `0.05` | RMS cible après gain |
| `max_gain` | float | `8.0` | Gain maximum en dB |
| `loudnorm_after_gain` | bool | `true` | Appliquer loudnorm après le gain |
| `target_i` | float | `-23.0` | Integrated loudness cible (LUFS) |
| `true_peak` | float | `-2.0` | True peak cible (dBTP) |
| `lra` | float | `11.0` | Loudness Range cible (LU) |

**Auto-loudnorm :** même si `enabled=false`, le pipeline force une normalisation `loudnorm`
lorsque le RMS de l'audio est inférieur à `auto_loudnorm_rms_threshold` (défaut 0.02).
Ce mécanisme évite que la VAD Silero rejette tout l'audio comme non-vocal sur un signal
trop silencieux (voix chuchotée, micro lointain). L'artefact `metadata/audio_normalization.json`
contient alors `"forced": true` avec `"reasons": ["audio_trop_silencieux_auto_loudnorm", "rms=0.00600"]`.

**Impact :** si la normalisation s'applique, `input/normalized.wav` remplace l'audio transmis au STT et `metadata/audio_normalization.json` documente les filtres. En cas d'erreur ffmpeg, l'audio original est conservé.

#### `workflow.source_separation`

Séparation de sources vocales via Demucs. Ne s'active **jamais automatiquement** :
c'est `SourceSeparationDecider` qui décide sur la base des signaux de
`audio_quality_decision.json` et `audio_scene`.

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
| `decision.scene_music_min_ratio` | number/null | `0.80` | Ratio musique suffisant pour forcer la séparation (relevé de 0.05 après benchmarks : réunions = 0.47–0.76 en faux positif) |
| `decision.scene_music_min_duration_s` | number/null | `60` | Durée musique suffisante pour forcer la séparation |
| `decision.scene_music_min_speech_ratio_for_force` | number/null | `0.08` | Si `music_ratio` dépasse le seuil mais `speech_ratio` est en dessous, la musique est ignorée (CSE complet : speech=0.015, music=0.98) |
| `decision.scene_noise_score_ratio` | number/null | `0.35` | Ratio bruit à partir duquel un score est ajouté |
| `decision.scene_noise_score` | int | `1` | Score ajouté si le bruit de scène dépasse le seuil |
| `decision.scene_problem_segments_score_threshold` | number/null | `3` | Nombre de zones problématiques au-delà duquel un score est ajouté |
| `decision.scene_problem_segments_score` | int | `1` | Score ajouté si le nombre de zones problématiques dépasse le seuil |

**Redémarrage requis :** non.

**Impact :** si `should_separate()` retourne `True`, la piste vocale extraite (`vocals.wav`) remplace l'audio d'entrée pour le reste du pipeline STT. En cas d'erreur Demucs, l'audio original est conservé sans interruption (dégradation gracieuse).

#### `workflow.transcription_cleanup`

Nettoyage déterministe post-STT appliqué après la transcription finale et le réalignement locuteurs.
Supprime les artefacts de sous-titrage récurrents, retire les hallucinations textuelles évidentes
observées sur les bancs audio, et fusionne les micro-segments courts d'un même locuteur.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Active le nettoyage post-STT |
| `merge_short_segments` | bool | `true` | Fusionne les segments courts (< seuils) avec le segment précédent si même locuteur |
| `remove_subtitle_artifacts` | bool | `true` | Supprime les artefacts de sous-titrage récurrents |
| `remove_obvious_hallucinations` | bool | `true` | Active le retrait déterministe des hallucinations textuelles évidentes |
| `remove_non_latin_hallucinations` | bool | `true` | Supprime les segments majoritairement hors alphabet attendu (arabe, CJK, cyrillique, coréen) |
| `remove_generic_hallucinations` | bool | `true` | Supprime les phrases génériques isolées connues, selon `generic_hallucination_languages` |
| `non_latin_char_pattern` | string | regex Unicode | Regex des caractères considérés hors alphabet latin attendu |
| `non_latin_min_chars` | int | `2` | Nombre minimal de caractères non latins avant filtrage |
| `non_latin_min_ratio` | float | `0.25` | Ratio minimal caractères non latins / lettres du segment pour supprimer |
| `generic_hallucination_languages` | list[string] | `["fr"]` | Langues de job où les phrases génériques anglaises isolées sont considérées comme hallucinations |
| `generic_hallucination_patterns` | list[regex] | `[]` | Liste de patterns regex. Liste vide = utiliser les patterns intégrés (`thank you`, `bye`, etc. isolés) |
| `isolated_noise_artifact_words` | list[string] | `["501"]` | Tokens isolés connus à supprimer seulement comme segment court autonome |
| `isolated_noise_artifact_max_s` | float | `0.8` | Durée maximale d'un token isolé avant suppression |
| `subtitle_artifact_patterns` | list[regex] | `[]` | Liste de patterns regex pour détecter les artefacts de sous-titrage. Liste vide = utiliser les patterns intégrés |
| `subtitle_artifact_words` | list[string] | `[]` | Liste de phrases courtes normalisées à filtrer. Liste vide = utiliser les mots-clés intégrés |
| `short_segment_max_s` | float | `0.45` | Durée maximale (s) pour qu'un segment soit considéré court |
| `short_segment_max_words` | int | `2` | Nombre maximal de mots pour qu'un segment soit considéré court |
| `merge_gap_s` | float | `0.5` | Durée maximale du gap (s) entre deux segments fusionnables |
| `merge_max_chars` | int | `220` | Nombre maximal de caractères du segment fusionné résultant |

Les artefacts de sous-titrage supprimés (`Sous-titrage ST' 501`, `FR 2021`, `Société Radio-Canada`, variantes tronquées) sont configurables via `subtitle_artifact_patterns` et `subtitle_artifact_words`. Si ces listes sont vides (défaut), les patterns et mots-clés intégrés au code sont utilisés.

Le retrait d'hallucinations reste volontairement conservateur : il ne supprime pas tous les segments `suspect/degrade`, seulement les segments à signal textuel fort (texte majoritairement non latin pour une réunion française, ou phrase générique isolée comme `thank you`). Les artefacts numériques courts comme `501` ne sont supprimés que s'ils forment un segment autonome très court ; un nombre dans une vraie phrase est conservé. Pour un job explicitement anglais, les phrases génériques anglaises isolées ne sont pas filtrées par défaut. L'opération est tracée dans les logs du pipeline (`removed_artifacts=N, removed_hallucinations=N, merged_short_segments=M`).

#### `workflow.stt_hybrid`

Contrat de configuration du futur mode qualité hybride Cohere→Whisper au segment.
Ce bloc est **désactivé par défaut** et n'est pas encore branché dans le pipeline
applicatif. Il ne doit pas être confondu avec `inference.mode=hybrid`, qui concerne
le placement local/distant des ressources GPU.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Active le mode hybride STT quand l'intégration pipeline sera livrée |
| `primary_backend` | string | `"cohere"` | Backend chemin rapide à conserver si la fenêtre est propre |
| `fallback_backend` | string | `"whisper"` | Backend de secours candidat sur zones non propres |
| `fallback_on_reliability` | list[string] | `["degrade"]` | Niveaux `reliability` candidats à une bascule automatique |
| `review_on_reliability` | list[string] | `["suspect"]` | Niveaux à envoyer en arbitrage LLM ou relecture humaine |
| `decision_margin` | number | `3` | Marge minimale de score pour accepter un fallback heuristique |
| `window_s` | number | `30.0` | Taille des fenêtres d'arbitrage prototype |
| `llm_arbitration_enabled` | bool | `false` | Autorise l'arbitrage LLM des fenêtres `review` |
| `write_audit_artifacts` | bool | `true` | Écrit les JSON/SRT/MD d'audit du mode hybride |

**État actuel :** les scripts `build_hybrid_transcript.py` et
`arbitrate_hybrid_llm.py` utilisent ce modèle de décision hors pipeline. Le
pipeline normal ignore encore `workflow.stt_hybrid`; par sécurité, le schéma
refuse `enabled: true` tant que l'activation produit n'est pas livrée avec
artefacts d'audit par job.

#### `workflow.speaker_realignment`

Réaligne les locuteurs au niveau mot quand les timestamps `words` Whisper/CTC
sont disponibles et qu'un segment ASR traverse plusieurs tours pyannote.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Active le réalignement locuteur mot-à-mot |
| `min_word_overlap_s` | float | `0.01` | Chevauchement minimal en secondes pour le réalignement |
| `punctuation_chars` | string | `".!?"` | Caractères de ponctuation déclenchant un réalignement |

#### `workflow.segment_reliability`

Scoring de fiabilité post-STT. Chaque segment reçoit un statut (`ok`, `suspect`, `degrade`)
basé sur les probabilités `no_speech_prob`, la confiance mot-à-mot et des flags textuels
configurables. Les segments `degrade` alimentent le score composite d'hallucination de
`QualityReporter`. Le moteur ne contient pas de termes métier codés en dur : les signatures
textuelles doivent être déclarées dans la configuration.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Activer le scoring de fiabilité segmentaire post-STT |
| `no_speech_prob_threshold` | float | `0.5` | Seuil no_speech_prob pour marquer un segment suspect |
| `low_word_confidence_ratio` | float | `0.5` | Proportion minimale de mots peu confiants pour signaler |
| `low_word_confidence_min` | float | `0.4` | Seuil de probabilité mot pour le flag « peu confiant » |
| `micro_segment_s` | float | `0.35` | Durée en secondes en dessous de laquelle un segment est « micro » |
| `short_segment_s` | float | `0.8` | Durée en secondes en dessous de laquelle un segment est « court » |
| `detect_non_latin` | bool | `true` | Active le flag `texte_non_latin` via une regex configurable |
| `non_latin_char_pattern` | string | regex Unicode | Regex des familles de caractères considérées hors alphabet latin attendu |
| `non_latin_min_chars` | int | `2` | Nombre minimal de caractères détectés avant de signaler le segment |
| `detect_generic_hallucinations` | bool | `true` | Active les regex configurées dans `generic_hallucination_patterns` |
| `generic_hallucination_patterns` | list[string] | liste configurable | Regex configurables pour signatures d'hallucination récurrentes connues ou observées localement ; inclut notamment les artefacts courts `thank you`/`thanks` observés sur audio français faible ou étroit |
| `degrade_on_text_flags` | bool | `true` | Classe directement en `degrade` un segment portant `texte_non_latin` ou `hallucination_generique` |

**Redémarrage requis :** non — lu à chaque pipeline.

#### `workflow.pyannote_chunking`

Paramètres de chunking par tours pyannote pour la transcription finale.
Contrôlent la fusion des micro-segments pyannote adjacents et le padding
autour des chunks transmis au backend STT.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `merge_micro_chunks` | bool | `true` | Fusionner les micro-segments pyannote adjacents |
| `micro_chunk_s` | float | `0.35` | Seuil de durée pour un micro-segment (secondes) |
| `micro_chunk_neighbor_gap_s` | float | `0.4` | Gap maximum entre deux micro-segments pour fusion (secondes) |
| `isolated_min_chunk_s` | float | `0.3` | Durée minimale pour un segment isolé non fusionné |
| `padding_s` | float | `0.15` | Padding autour des chunks pyannote (secondes). Les benches réunion 2026-06 ont montré qu'un padding `0.30` dégrade le texte sans améliorer le comptage locuteurs. |
| `max_chunk_s` | int | `45` | Durée maximale d'un chunk pyannote (secondes). Les benches réunion 2026-06 ont montré un gain de vitesse vs `30` sans perte texte/locuteurs sur les fenêtres de référence, avec `cohere.chunk_length_s=30`. |
| `min_chunk_s` | float | `1.5` | Durée minimale d'un chunk (secondes) |

**Redémarrage requis :** non — lu à chaque transcription.

`max_chunk_s` et `cohere.chunk_length_s` sont deux limites différentes. Le premier borne les tours pyannote transmis au backend STT ; le second borne le découpage interne de Cohere. Le couple validé par bench est `45/30`. Ne pas interpréter l'ancien essai `workflow.pyannote_chunking.max_chunk_s=35` comme une validation de `cohere.chunk_length_s=35`.

### `diarization`

Paramètres de cache pour éviter de relancer pyannote quand l'audio et le modèle
n'ont pas changé.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `cache_enabled` | bool | `true` | Réutilise `speaker_turns.json` si le checkpoint correspond |
| `cache_audio_fingerprint` | bool | `true` | Vérifie taille/mtime/chemin de l'audio avant réutilisation |
| `embedding_cache_enabled` | bool | `true` | Écrit un checkpoint acoustique par locuteur |
| `embedding_clip_seconds` | float | `12.0` | Durée maximale utilisée par locuteur pour le checkpoint |
| `preload_audio` | bool | `true` | Demande à pyannote de charger l'audio en RAM avant l'inférence. Accélère les réunions longues et les formats compressés en évitant les décodages répétés pendant l'extraction d'embeddings. |
| `prepare_pcm_audio` | bool | `false` | Prépare un WAV PCM 16 kHz mono réservé à pyannote avant l'inférence. Optimisation best-effort : l'audio original est conservé si ffmpeg échoue ou si la durée source/cible diverge. |
| `prepare_pcm_timeout_s` | int \| null | `1800` | Timeout ffmpeg de préparation PCM pyannote. |
| `prepare_pcm_duration_tolerance_s` | float \| null | `0.25` | Écart maximal accepté entre la durée source et la durée WAV préparée. |
| `embedding_batch_size` | int \| null | `64` | Taille des lots d'embeddings pyannote. `64` est un défaut prudent ; `96`/`128` sont des candidats de bench sur GPU avec forte marge VRAM. |
| `segmentation_batch_size` | int \| null | `null` | Taille des lots de segmentation pyannote. `null` conserve la valeur du pipeline HF ; la segmentation n'est généralement pas le goulet observé. |
| `progress_log_enabled` | bool | `true` | Active le hook de progression pyannote et les logs de sous-étapes longues |
| `progress_log_interval_s` | float | `30.0` | Intervalle minimal entre deux logs d'avancement d'une même étape pyannote |
| `min_speakers` | int \| null | `2` | Nombre minimal de locuteurs transmis à pyannote si `num_speakers` est absent |
| `max_speakers` | int \| null | `20` | Nombre maximal de locuteurs transmis à pyannote si `num_speakers` est absent |
| `num_speakers` | int \| null | `null` | Nombre exact de locuteurs, prioritaire sur `min_speakers`/`max_speakers` |

**Override par job (fourchette UI) :** ces valeurs globales peuvent être surchargées par job. L'étape Résumé du wizard propose un champ optionnel min/max locuteurs, stocké dans `jobs.extra_data_json["speaker_hint"]`. À la diarisation, `diarizer_factory.apply_speaker_hint()` écrit `min_speakers`/`max_speakers` (et `num_speakers` si min == max) depuis ce hint, et bascule `models.diarization_backend` de `sortformer` vers `pyannote` si la borne haute saisie dépasse 4 (capacité Sortformer). Le hint ne s'applique qu'au job concerné ; la config globale reste inchangée.

**Performance pyannote :** sur les longues réunions, le coût principal observé est `embeddings`, pas `segmentation`. `preload_audio=true` réduit les recoupes/décodages audio répétés. `prepare_pcm_audio=true` ajoute un cache WAV 16 kHz mono uniquement pour pyannote ; il conserve la timeline et refuse le fichier préparé si la durée ne correspond pas. `embedding_batch_size` réduit le nombre de lots pyannote, mais une valeur trop haute peut augmenter la VRAM et provoquer un OOM ; valider `96` ou `128` par bench avant d'en faire un réglage de production.

#### `diarization.pipeline_params`

Paramètres internes pyannote expérimentaux. Ils sont appliqués via `Pipeline.instantiate()`
avant l'inférence et inclus dans le checkpoint de cache. À laisser à `null` tant qu'un
bench de référence n'a pas validé le réglage sur le corpus cible.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `segmentation.min_duration_off` | float \| null | `null` | Durée minimale de silence entre deux tours. Community-1 est powerset : `segmentation.threshold` n'est pas exposé par notre version. |
| `clustering.threshold` | float \| null | `null` | Seuil VBx de clustering. Piste prioritaire pour tester le sous/sur-comptage en mode nombre inconnu. |
| `clustering.Fa` | float \| null | `null` | Paramètre VBx interne, expérimental. |
| `clustering.Fb` | float \| null | `null` | Paramètre VBx interne, expérimental. |

Résultat de calibration 2026-06 : sur les fenêtres de référence d'une réunion dense,
`clustering.threshold=0.50/0.55/0.65` n'a pas changé le comptage locuteurs ni le
texte par rapport au mode pyannote automatique. Le nombre exact via
`diarization.num_speakers` reste le seul réglage mesuré qui corrige totalement le
comptage sur ce corpus.

#### `workflow.execution`

Configuration du worker interne qui exécute les traitements longs hors requête HTTP.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `max_concurrent_jobs` | int | `1` | Nombre maximal de jobs exécutés en parallèle par le worker interne |

**Redémarrage requis :** oui — le worker est instancié au démarrage de l’application.

**Note :** la valeur par défaut `1` est volontaire sur un service GPU partagé. Monter plus haut sans revoir la stratégie VRAM augmentera fortement le risque de contention et d’échec.

#### `workflow.concurrency_profile`

**B8 — observabilité du goulot.** Surcharges déclaratives de la classe d'une étape du workflow,
exposées dans `GET /api/resources/status` (clé `concurrency` : % sériel, étape goulot, attente
estimée). Vide = la classe est **dérivée automatiquement** (STT distant `concurrent_safe` =
*delegated*, sinon *serial*). Voir [`CONCURRENCE_ET_CHARGE_PHASE_B.md`](CONCURRENCE_ET_CHARGE_PHASE_B.md) §C7.

| Clé | Type | Description |
|---|---|---|
| `<étape>.class` | string | `serial` (ressource exclusive) \| `delegated` (capacité fixée par le backend opérateur) |
| `<étape>.resource` | string | `gpu` \| `cpu` \| `llm` \| `stt_backend` (étiquette de la ressource arbitrée) |

Étapes connues : `transcribe`, `diarization`, `voice_embed`, `correction`, `quality`, `export`.
Ex. : `{"transcribe": {"class": "delegated", "resource": "stt_backend"}}`. Purement indicatif —
aucune orchestration n'en découle (le multi-concurrence reste géré par les scripts de l'opérateur).

#### `workflow.progress`

Progression utilisateur persistée dans `jobs.extra_data_json["workflow_progress"]`
et exposée par `GET /api/jobs/<id>/status`. Ce canal est distinct des logs techniques :
messages courts, non confidentiels, et écritures DB throttlées.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Active la progression détaillée affichée dans le wizard pendant les traitements longs |
| `update_interval_s` | float | `10.0` | Intervalle minimal entre deux écritures DB non forcées pour un même job |

#### `workflow.queue`

Configuration de la file persistante et du scheduler applicatif. Quand elle est activée, `/api/jobs/<id>/process` écrit dans `job_queue`, puis `QueueScheduler` lance le pipeline dès que les conditions sont réunies.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Active la file persistante. `false` garde le chemin direct historique du worker |
| `default_priority` | int | `50` | Priorité appliquée si aucune priorité n'est fournie. Plus petit = plus prioritaire |
| `aging_enabled` | bool | `true` | Active le bonus progressif des jobs en attente |
| `aging_interval_minutes` | int | `30` | Intervalle d'attente donnant un point de bonus |
| `aging_max_bonus` | int | `49` | Bonus maximal soustrait à la priorité effective |
| `poll_interval_s` | int | `5` | Délai entre deux itérations de dispatch (latence max de prise en file sans `NOTIFY`) |
| `use_listen_notify` | bool | `false` | **B9** : réveil instantané de l'ordonnanceur via PostgreSQL `LISTEN/NOTIFY`. À activer quand les rôles `web` et `scheduler` sont des process séparés et que la latence du poll gêne. PostgreSQL requis ; le polling reste le filet de sûreté |
| `starvation_timeout_hours` | int | `24` | Seuil d'alerte de famine d'un job en attente |

**Redémarrage requis :** oui pour `enabled`, `poll_interval_s`, `use_listen_notify` et les paramètres de worker/scheduler, car `JobExecutorService` et `QueueScheduler` sont instanciés au démarrage. Les priorités passées à l'API sont persistées avec chaque entrée de file.

**Permissions :** les admins globaux peuvent gérer toute la file. Les admins de groupe peuvent gérer uniquement les jobs appartenant aux membres de leurs groupes. Les mutations sont auditées.

#### `workflow.scheduling`

Configuration générale du calendrier. Les créneaux eux-mêmes sont stockés en base dans `scheduling_windows` et modifiables depuis `/admin/schedule`.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Active la prise en compte des créneaux |
| `timezone` | string | `"Europe/Paris"` | Fuseau horaire utilisé pour évaluer les jours et heures |
| `kill_patterns` | list[str] | `["vllm", "llama-server", ...]` | Patterns de processus externes que `force_gpu` et la libération VRAM ciblée peuvent tuer ; les autres processus GPU sont ignorés |
| `windows` | list[dict] | `[]` | Valeurs initiales/documentaires ; le runtime utilise la table `scheduling_windows` |

Format d'un créneau :

```yaml
workflow:
  scheduling:
    enabled: true
    timezone: Europe/Paris
```

Chaque ligne `scheduling_windows` contient `name`, `days`, `start`, `end`, `action`, `action_params` et `enabled`. Les jours sont en français (`lundi` à `dimanche`) et les horaires au format `HH:MM`. Dans l'interface, `action` est présenté comme une **règle appliquée** au créneau.

Règles supportées :

| Action | Type | Effet |
|---|---|
| `pause_queue` | on/off | Suspend le dispatch des nouveaux jobs ; les jobs en cours continuent |
| `limit_concurrency` | paramétrée | Réduit la concurrence effective via `action_params.max_concurrent_jobs` |
| `force_gpu` | on/off | Autorise `GPUAllocator.force_free_gpu(..., allow_kill=True)` sur les patterns explicitement configurés |
| `none` | on/off | Aucun effet, utile comme note de calendrier |

Si plusieurs créneaux sont actifs, la priorité est `pause_queue` > `limit_concurrency` > `force_gpu` > `none`. Les créneaux traversant minuit sont supportés.
`force_gpu` cible le GPU visible choisi par l'allocateur et résout l'index physique pour `nvidia-smi` si `CUDA_VISIBLE_DEVICES` est défini. Aucun processus hors `workflow.scheduling.kill_patterns` ne doit être tué.

Le calendrier ne configure pas un nombre de GPUs. La LLM d'arbitrage peut occuper plusieurs GPUs et la disponibilité réelle dépend des phases pipeline ; l'arbitrage fiable reste donc dans `GPUAllocator`.

#### `workflow.summary_llm`

Configuration de la LLM de résumé.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Active la Phase 2 LLM du résumé ; à activer seulement quand `model_id` est renseigné |
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
| `max_upload_size_mb` | int | `1024` | Taille maximale d'upload Flask (`MAX_CONTENT_LENGTH`) en Mio |
| `allowed_upload_extensions` | list[str] | `[".mp3", ".wav", ".m4a", ".mp4", ".flac", ".ogg"]` | Extensions autorisées pour l'upload |
| `audit_retention_days` | int | `1095` | Durée de rétention des logs d'audit (3 ans). Distinct de `retention_days` qui concerne les jobs. |
| `lexicon_export_admin_only` | bool | `false` | Réserve l'export CSV des lexiques centralisés aux admins globaux. Les admins de groupe peuvent toujours gérer les entrées de leur périmètre. |
| `audit_retention_by_family` | dict | toutes familles à `1095` | Surcharge optionnelle de rétention par famille d'audit : `auth`, `job`, `lexicon`, `voice`, `config`, `other`. |

**Redémarrage requis :** oui pour `max_upload_size_mb` (chargé dans `create_app()`), non pour `retention_days`, `allow_job_delete`, `allowed_upload_extensions`, `audit_retention_days`, `lexicon_export_admin_only` et `audit_retention_by_family` qui sont vérifiés à l'exécution.

**Impact si modifié :**
- `retention_days` : appliqué par `JobStore.purge_expired_jobs()` lors de l'accès à la page d'accueil. Seuls les jobs anciens en état terminal sont supprimés avec leurs fichiers.
- `allow_job_delete=false` : la route `delete_job` retourne 403. La suppression est bloquée même pour l'admin.
- `max_upload_size_mb` : limite les uploads HTTP côté Flask. Une valeur trop basse bloque les fichiers audio longs avec une erreur 413.
- `allowed_upload_extensions` : extensions vérifiées dans `api_upload`. Les extensions doivent inclure le point (`.mp3`, pas `mp3`).
- `audit_retention_days` : rétention par défaut appliquée par `AuditStore.purge_expired_by_policy()` à chaque accès à la page d'accueil. Valeurs typiques : 365 (1 an), 1095 (3 ans, défaut), 1825 (5 ans) selon la politique de conservation.
- `lexicon_export_admin_only=true` : la route `POST /admin/lexicons/<id>/export.csv` retourne 403 aux admins de groupe ; l'UI affiche un badge explicite à la place du bouton d'export.
- `audit_retention_by_family` : permet de raccourcir ou prolonger une famille précise sans changer le défaut global. Exemple : `lexicon: 365` si la politique DPO/PSSI impose une durée spécifique aux événements de référentiels lexiques.

---

### `voice_enrollment`

Référentiel local de voix connues avec consentement explicite. Désactivé par défaut.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Active la gestion des voix enregistrées |
| `storage_dir` | string | `"./voices"` | Répertoire runtime sensible pour preuves et audios temporaires |
| `require_active_consent` | bool | `true` | Bloque la vectorisation sans consentement actif |
| `delete_source_audio_after_embedding` | bool | `true` | Supprime l'audio source après génération d'empreinte |
| `allow_global_profiles` | bool | `false` | Autorise des voix sans groupe, admins globaux uniquement |
| `require_explicit_job_group_for_multi_group_users` | bool | `true` | Empêche un périmètre implicite multi-groupe |

#### `voice_enrollment.embedding`

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `backend` | string | `"pyannote"` | Backend d'embedding vocal |
| `model_id` | string | `"pyannote/speaker-diarization-community-1"` | Modèle utilisé pour l'empreinte |
| `model_revision` | string | `""` | Révision modèle si disponible |
| `expected_dim` | int/null | `null` | Dimension attendue, utilisée pour détecter les changements |
| `normalization` | string | `"l2"` | Normalisation appliquée avant stockage |
| `min_speech_duration_s` | float | `8.0` | Durée minimale recommandée |
| `min_segment_duration_s` | float | `1.5` | Segment trop court ignoré par le futur matching |
| `max_segments_per_speaker` | int | `5` | Nombre maximal de segments utilisés |
| `exclude_overlap` | bool | `true` | Évite les zones chevauchées si disponibles |

#### `voice_enrollment.matching`

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `enabled_after_summary` | bool | `false` | Réservé à un déclenchement automatique futur ; la V1 expose un bouton manuel |
| `suggestion_threshold` | float | `0.72` | Score cosinus minimal pour proposer une voix |
| `high_confidence_threshold` | float | `0.86` | Score à partir duquel la suggestion est marquée haute confiance |
| `min_top2_margin` | float | `0.05` | Écart minimal entre le premier et le deuxième candidat |
| `max_candidates_per_speaker` | int | `2` | Nombre maximal de candidats conservés pour audit et diagnostic |
| `stale_profiles_are_matchable` | bool | `false` | Autorise exceptionnellement les profils périmés ; désactivé par défaut |

#### `voice_enrollment.consent`

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `current_form_version` | string | `"voice-consent-v1"` | Version du formulaire de consentement |
| `allow_expiration` | bool | `false` | Réservé à une future expiration automatique |
| `validity_days` | int/null | `null` | Durée de validité si expiration activée |
| `proof_allowed_extensions` | list[str] | `["pdf", "png", "jpg", "jpeg"]` | Extensions des preuves signées |
| `max_proof_size_mb` | int | `25` | Taille maximale d'une preuve |

---

### `inference`

Inférence distante : permet à TranscrIA d'être une **frontale** dont les ressources GPU (STT,
diarisation, empreinte vocale) tournent sur un nœud distant — ou sur la même machine via 127.0.0.1.
`mode: "local"` (défaut) = tout local, aucun appel réseau, comportement historique préservé.
Détail : [`SERVICE_RESSOURCES_GPU.md`](SERVICE_RESSOURCES_GPU.md).

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `mode` | string | `"local"` | `local` \| `remote` \| `hybrid`. Active la sélection distante (diarize/voice-embed + STT par backend) |
| `url` | string | `""` | URL du service Flask de ressources (diarize/voice-embed), ex. `http://HOST:8002`. Vide = local |
| `nodes` | list | `[]` | **Failover actif/passif (B7)** : liste ordonnée `[{url, priority}]` (priorité = ordre). La frontale vise le premier nœud joignable et bascule automatiquement ; quand le principal revient, les jobs y repartent. Vide = un seul nœud (`url`) |
| `fallback_local` | bool | `true` | Bascule locale si le service distant tombe (sauf 4xx définitif) |
| `auth.api_key_env` | string | `"TRANSCRIA_INFERENCE_API_KEY"` | Variable d'env portant la clé API du service |
| `auth.api_key` | string | `""` | Clé API en clair (priorité à la variable d'env) |
| `transport.audio` | string | `"file_ref"` | `file_ref` (chemin, mono-machine/FS partagé) \| `upload` (octets, **obligatoire en vrai distant**) |
| `resilience.timeout_s` | int | `1800` | Timeout par requête au service |
| `resilience.retries` | int | `2` | Tentatives sur erreur transitoire (5xx/503/réseau) |
| `resilience.max_unavailable_s` | int | `600` | Mode dégradé §7.2 : au-delà, un job dont les ressources distantes sont injoignables échoue explicitement (en deçà : mis en file) |
| `resilience.capabilities_cache_ttl_s` | int/float | `5` | **B6.4** : TTL du cache de `GET /api/resources/status` (mutualise les appels `/capabilities` entre clients web). `0` = désactivé |

#### `inference.stt`

STT via un serveur compatible OpenAI (`/v1/audio/transcriptions`) — moteur **non hardcodé** (vLLM, SGLang…).

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `fallback_local` | bool | `true` | Bascule sur le transcripteur local si le serveur STT tombe |
| `response_format` | string | `"verbose_json"` | Défaut global ; `verbose_json` (segments) \| `json` (texte). Surchargeable par backend |
| `collapse_repetition_loops` | bool | `true` | Anti-hallucination (parité avec les backends locaux) |
| `concurrency` | int | `1` | Tours transcrits en parallèle (>1 = distant uniquement, exploite le batching vLLM). 1 = séquentiel |
| `timeout_s` | int | `600` | Timeout par requête STT |
| `retries` | int | `2` | Tentatives sur erreur transitoire |
| `auth.api_key_env` / `auth.api_key` | string | `"TRANSCRIA_STT_API_KEY"` / `""` | Clé API du serveur STT (si lancé avec `--api-key`) |

#### `inference.stt.backends.<moteur>`

Un endpoint par moteur logique (`cohere`, `whisper`). **`url` vide = ce moteur reste local** même en mode remote/hybrid.

| Paramètre | Type | Défaut (cohere / whisper) | Description |
|---|---|---|---|
| `url` | string | `""` / `""` | Racine OpenAI, ex. `http://127.0.0.1:8003/v1`. Vide = local |
| `model` | string | `cohere-transcribe` / `whisper-large-v3` | `served-model-name` attendu par le serveur |
| `response_format` | string | `json` / `verbose_json` | Cohere Transcribe (vLLM) refuse `verbose_json` (400) → `json` |

#### `resource_node` (config du nœud de ressources uniquement)

Manifeste lu côté nœud (pas dans les défauts ; absent = aucun moteur géré). Voir `scripts/launch_stt_*.sh`.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `vram.preflight` | bool | `true` | Pré-check VRAM avant lancement (refuse proprement au lieu d'OOM) |
| `vram.auto_relocate` | bool | `false` | Repli sur un autre GPU si l'assigné est plein (log bruyant) |
| `engines[]` | list | `[]` | Moteurs déclarés : `{name, script, gpu, gpu_mem, port, idle_timeout_s}` (placement = admin). `idle_timeout_s > 0` active l'idle-stop opportuniste de ce moteur (défaut `0` = résident) |

---

## Section `notifications`

### `notifications.email` — Notifications par email

Envoie un email à l'utilisateur propriétaire du job à la fin du traitement (succès ou échec). Les emails contiennent un lien direct vers la transcription.

> Les emails sont envoyés de façon asynchrone (fil de fond daemon) : ils ne bloquent jamais le pipeline.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Active/désactive les notifications email |
| `smtp_host` | string | `""` | Serveur SMTP (ex : `smtp.gmail.com`) |
| `smtp_port` | int | `587` | Port SMTP : `587` (STARTTLS), `465` (SSL), `25` (nu) |
| `smtp_username` | string | `""` | Identifiant SMTP (laisser vide si pas d'auth) |
| `smtp_password` | string | `""` | Mot de passe SMTP |
| `use_starttls` | bool | `true` | Chiffrement STARTTLS — recommandé pour le port 587 |
| `use_ssl` | bool | `false` | SMTPS (SSL dès la connexion) — pour le port 465 |
| `from_address` | string | `""` | Adresse expéditeur (ex : `transcria@example.com`) |
| `from_name` | string | `"TranscrIA"` | Nom affiché dans le champ « De : » |
| `base_url` | string | `"http://localhost:7870"` | URL publique du serveur, utilisée pour les liens dans les emails |

**Modes de chiffrement :**

| Mode | `smtp_port` | `use_starttls` | `use_ssl` |
|---|---|---|---|
| STARTTLS (recommandé) | `587` | `true` | `false` |
| SMTPS / SSL | `465` | `false` | `true` |
| SMTP nu (intranet) | `25` | `false` | `false` |

**Prérequis utilisateurs :** l'adresse email doit être renseignée dans le profil de chaque utilisateur (section *Gestion des utilisateurs* de l'interface d'administration).

---

## 6. Variables d'environnement

| Variable | Description | Défaut si absente |
|---|---|---|
| `TRANSCRIA_CONFIG` | Chemin vers le fichier config.yaml | `config.yaml` |
| `TRANSCRIA_SECRET` | Clé secrète Flask (sessions) | `os.urandom(32).hex()` (aléatoire à chaque redémarrage) |
| `TRANSCRIA_DATABASE_URL` | DSN base de données (prioritaire sur `storage.database_url`). Garde le mot de passe hors config versionnée. Ex. `postgresql+psycopg://transcria:***@127.0.0.1:5432/transcria` | Valeur de `storage.database_url` |
| `TRANSCRIA_ROLE` | Rôle du process (montée en charge, Phase B) : `all` \| `web` \| `scheduler`. Prioritaire sur `runtime.role` | Valeur de `runtime.role` (= `all`) |
| `TRANSCRIA_INFERENCE_API_KEY` | Clé API du nœud de ressources (diarize/voice-embed), si lancé avec auth | — (aucune auth) |
| `TRANSCRIA_STT_API_KEY` | Clé API du serveur STT OpenAI-compat, si lancé avec `--api-key` | — (aucune auth) |
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
| `gpu.sortformer_vram_mb` | `3500` | VRAM estimée Sortformer (NeMo) — lue par `get_diarizer_vram_mb("sortformer", config)` |
| `gpu.granite_vram_mb` | `6000` | VRAM estimée Granite |
| `gpu.parakeet_vram_mb` | `8000` | VRAM estimée Parakeet (NeMo + buffers) |
| `gpu.llm_vram_mb` | `60000` | VRAM estimée LLM |
| `gpu.min_free_vram_mb` | `4000` | VRAM minimale libre |

`CUDA_VISIBLE_DEVICES` est supporté pour les runs isolés : les ids physiques remontés par le dashboard sont remappés vers les ordinaux CUDA visibles avant chargement modèle. `CUDA_VISIBLE_DEVICES=-1` désactive la sélection GPU. `TRANSCRIA_PREFERRED_GPU` désigne alors un ordinal visible, pas forcément l'id physique.

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

#### `quality.thresholds`

Seuils de détection des segments suspects dans `QualityReporter`. Ces checks alimentent le rapport qualité et le score composite d'hallucination sans modifier le SRT.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `no_speech_prob_threshold` | float | `0.5` | Seuil au-dessus duquel un segment est signalé comme suspect (no_speech_prob trop élevé) |
| `low_word_confidence_ratio` | float | `0.5` | Proportion minimale de mots peu confiants pour signaler un segment |
| `low_word_confidence_min` | float | `0.4` | Seuil de probabilité mot en-dessous duquel un mot est considéré peu confiant |

---

## 10. Fichiers de prompts opencode (configs/prompts/)

| Fichier | Utilisé par | Description |
|---|---|---|
| `summary_prompt.txt` | `OpenCodeRunner.run_summary()` | Prompt système pour le résumé structuré v2.0 (394 lignes) |
| `correction_prompt.txt` | `OpenCodeRunner.run_correction()` | Prompt système pour la correction SRT v1.9 (612 lignes) |

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
| `workflow.audio_normalization.*` | Non | Oui (PipelineService) |
| `workflow.audio_normalization.weak_voice.*` | Non | Oui (PipelineService) |
| `workflow.audio_preflight.*` | Non | Oui (PipelineService) |
| `workflow.audio_denoise.*` | Non | Oui (PipelineService) |
| `workflow.audio_scene.*` | Non | Oui (PipelineService) |
| `workflow.audio_scene_filter.*` | Non | Oui (PipelineService) |
| `workflow.source_separation.*` | Non | Oui (PipelineService) |
| `workflow.transcription_cleanup.*` | Non | Oui (Transcriber) |
| `workflow.stt_hybrid.*` | Non | Non encore consommé (contrat futur) |
| `workflow.segment_reliability.*` | Non | Oui |
| `workflow.pyannote_chunking.*` | Non | Oui |
| `workflow.summary_llm.*` | Non | Oui (SummaryGenerator, OpenCodeRunner) |
| `workflow.arbitration_llm.*` | Non | Oui |
| `quality.asr_noise_markers` | Non | Oui |
| `security.retention_days` | Non | Oui (purge à l'accueil) |
| `security.allow_job_delete` | Non | Oui (route) |
| `security.max_upload_size_mb` | Oui | Non (Flask `MAX_CONTENT_LENGTH`) |
| `security.allowed_upload_extensions` | Non | Oui (route) |
| `security.audit_retention_days` | Non | Oui (purge à l'accueil) |
| `security.lexicon_export_admin_only` | Non | Oui (route/UI lexiques) |
| `security.audit_retention_by_family` | Non | Oui (purge à l'accueil) |

**Problème architecturel :** `get_config()` retourne un singleton. Si `set_config()` est appelé pour mettre à jour une valeur, les routes qui rappellent `get_config()` voient le changement, mais les objets déjà construits avec une ancienne config ne sont pas mis à jour automatiquement.

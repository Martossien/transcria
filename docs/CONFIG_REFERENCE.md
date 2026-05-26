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
- `dashboard_llm_url` : utilisé par `VRAMManager` pour interroger l'API GPU (`/api/v1/gpus`). Si le dashboard est indisponible, `VRAMManager` bascule sur `nvidia-smi`.
- `srt_editor_easy_url` : utilisé pour le bouton "Ouvrir dans SRT Editor" et l'API `push-to-editor`. Si l'URL est incorrecte, le bouton apparaît mais la redirection échoue.

---

### `models`

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `stt_backend` | string | `"cohere"` | Backend STT (`cohere`, `whisper`, `granite` ou `parakeet`) |
| `default_stt_model` | string | `"cohere-transcribe-03-2026"` | Modèle STT par défaut |
| `fallback_stt_model` | string | `"large-v3"` | Modèle fallback |
| `cohere_model_path` | string | `"./models/cohere-asr/cohere-transcribe-03-2026"` | Chemin vers le modèle Cohere ASR local |
| `pyannote_model` | string | `"pyannote/speaker-diarization-community-1"` | Nom du modèle pyannote HuggingFace |

**Redémarrage requis :** non — les chemins sont lus à chaque transcription/diarization.

**Impact si modifié :**
- `cohere_model_path` : si le chemin est invalide, `CohereTranscriber.load()` échoue avec un avertissement. Le chemin est résolu en absolu si c'est un répertoire local (`os.path.abspath`). Si le chemin commence par `CohereLabs/` ou `cohere/`, HuggingFace download est utilisé.
- `pyannote_model` : doit être un modèle HuggingFace valide. Nécessite d'accepter les conditions sur huggingface.co et configurer `HF_TOKEN` pour les modèles gated.
- `stt_backend` pilote la sélection du backend via `TranscriberFactory`.

### `cohere`

Paramètres optionnels du backend Cohere ASR. Ces paramètres ne sont lus que si une section `[cohere]` existe dans `config.yaml`. En l'absence de cette section, les valeurs par défaut sont utilisées par `CohereTranscriber`.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `chunk_length_s` | int | `30` | Durée des chunks ASR en secondes |
| `max_new_tokens` | int | `448` | Nombre maximal de tokens générés par chunk |
| `repetition_penalty` | float | `1.2` | Pénalité de répétition pour Cohere |
| `no_repeat_ngram_size` | int | `3` | Taille des n-grams bloqués |
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
Supprime les artefacts de sous-titrage récurrents et fusionne les micro-segments courts d'un même locuteur.

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Active le nettoyage post-STT |
| `merge_short_segments` | bool | `true` | Fusionne les segments courts (< seuils) avec le segment précédent si même locuteur |
| `remove_subtitle_artifacts` | bool | `true` | Supprime les artefacts de sous-titrage récurrents |
| `subtitle_artifact_patterns` | list[regex] | `[]` | Liste de patterns regex pour détecter les artefacts de sous-titrage. Liste vide = utiliser les patterns intégrés |
| `subtitle_artifact_words` | list[string] | `[]` | Liste de phrases courtes normalisées à filtrer. Liste vide = utiliser les mots-clés intégrés |
| `short_segment_max_s` | float | `0.45` | Durée maximale (s) pour qu'un segment soit considéré court |
| `short_segment_max_words` | int | `2` | Nombre maximal de mots pour qu'un segment soit considéré court |
| `merge_gap_s` | float | `0.5` | Durée maximale du gap (s) entre deux segments fusionnables |
| `merge_max_chars` | int | `220` | Nombre maximal de caractères du segment fusionné résultant |

Les artefacts de sous-titrage supprimés (`Sous-titrage ST' 501`, `FR 2021`, `Société Radio-Canada`, variantes tronquées) sont configurables via `subtitle_artifact_patterns` et `subtitle_artifact_words`. Si ces listes sont vides (défaut), les patterns et mots-clés intégrés au code sont utilisés. L'opération est tracée dans les logs du pipeline (`removed_artifacts=N, merged_short_segments=M`).

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
| `padding_s` | float | `0.15` | Padding autour des chunks pyannote (secondes) |
| `max_chunk_s` | int | `30` | Durée maximale d'un chunk (secondes) |
| `min_chunk_s` | float | `1.5` | Durée minimale d'un chunk (secondes) |

**Redémarrage requis :** non — lu à chaque transcription.

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

**Redémarrage requis :** oui pour `max_upload_size_mb` (chargé dans `create_app()`), non pour `retention_days`, `allow_job_delete` et `allowed_upload_extensions` qui sont vérifiés à l'exécution.

**Impact si modifié :**
- `retention_days` : appliqué par `JobStore.purge_expired_jobs()` lors de l'accès à la page d'accueil. Seuls les jobs anciens en état terminal sont supprimés avec leurs fichiers.
- `allow_job_delete=false` : la route `delete_job` retourne 403. La suppression est bloquée même pour l'admin.
- `max_upload_size_mb` : limite les uploads HTTP côté Flask. Une valeur trop basse bloque les fichiers audio longs avec une erreur 413.
- `allowed_upload_extensions` : extensions vérifiées dans `api_upload`. Les extensions doivent inclure le point (`.mp3`, pas `mp3`).

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
| `gpu.granite_vram_mb` | `6000` | VRAM estimée Granite |
| `gpu.parakeet_vram_mb` | `8000` | VRAM estimée Parakeet (NeMo + buffers) |
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
| `workflow.segment_reliability.*` | Non | Oui |
| `workflow.pyannote_chunking.*` | Non | Oui |
| `workflow.summary_llm.*` | Non | Oui (SummaryGenerator, OpenCodeRunner) |
| `workflow.arbitration_llm.*` | Non | Oui |
| `quality.asr_noise_markers` | Non | Oui |
| `security.retention_days` | Non | Oui (purge à l'accueil) |
| `security.allow_job_delete` | Non | Oui (route) |
| `security.max_upload_size_mb` | Oui | Non (Flask `MAX_CONTENT_LENGTH`) |
| `security.allowed_upload_extensions` | Non | Oui (route) |

**Problème architecturel :** `get_config()` retourne un singleton. Si `set_config()` est appelé pour mettre à jour une valeur, les routes qui rappellent `get_config()` voient le changement, mais les objets déjà construits avec une ancienne config ne sont pas mis à jour automatiquement.

# Test E2E TranscrIA

## Objectif

`tests/test_e2e_workflow.py` exécute un job réel avec le même enchaînement que le workflow
applicatif. Il est conçu pour deux usages :

- **Validation manuelle** : tester une configuration spécifique et inspecter les artefacts
- **Benchmark automatisé** : appelé par `scripts/bench_audio.py` pour mesurer toutes les
  combinaisons d'options. Les plans de campagne passés sont archivés hors documentation active.

`tests/test_voice_e2e.py` couvre le parcours applicatif de la feature **Voix enregistrées** sans GPU réel : téléchargement du PDF vierge, création d'une voix avec genre validé, upload du consentement signé, génération d'une empreinte mockée, matching d'un locuteur de job et affichage de la suggestion dans l'étape Participants & Locuteurs.

`tests/test_central_lexicon.py` couvre le parcours applicatif des **lexiques centralisés** sans GPU réel : droits admin/admin groupe, création de lexique, import/édition d'entrées, périmètre job→groupes, sélection des lexiques cochés, pré-remplissage avec raison d'affichage, stats d'usage, contrôles qualité et filtrage du lexique avant correction.

`tests/test_stt.py` et `tests/test_workflow_runner.py` couvrent aussi le biasing STT expérimental depuis le lexique : hotwords Whisper bornés, activation uniquement quand le backend effectif est Whisper, audit dans `metadata/whisper_hotwords.json` ; sélection des formes cibles validées pour le Trie Cohere, sans booster les variantes fautives, et audit dans `metadata/cohere_lexicon_biasing.json`.

### Enchaînement

1. `JobService.create/upload/analyze`
2. `WorkflowRunner.run_summary()` : STT rapide (Cohere par défaut), pyannote, résumé LLM (sauf `--skip-llm`)
   - pyannote détecte les locuteurs + attribut le genre par locuteur
     (gender_segments × tours → `speaker_stats.json`, champ `gender`)
   - `_write_diarization_context` : section "Genre vocal par locuteur" dans
     `summary/diarization_context.md`
3. `MeetingContextManager` / `ParticipantsManager` / `LexiconManager`
   - l'étape lexique peut être pré-remplie par les lexiques centralisés cochés pour le job, après préfiltrage d'affichage
4. `SpeakerDetector.save_mapping()` + application des rôles LLM (`_apply_speaker_roles`)
5. `PipelineService.run_process(..., mode=<fast|quality>)` :
   - Analyse de scène audio (subprocess librosa) → `metadata/audio_scene.json`
   - Décision qualité audio → `metadata/audio_quality_decision.json`
   - Séparation de sources optionnelle (Demucs) → `input/vocals.wav`
   - Filtrage scène optionnel → `input/scene_filtered.wav` + `metadata/audio_scene_filter.json`
   - Normalisation audio optionnelle (y compris auto-loudnorm si RMS < seuil)
     → `input/normalized.wav` + `metadata/audio_normalization.json`
   - Transcription finale (Cohere ou Whisper large-v3)
   - Diarisation pyannote (mode quality uniquement)
   - VAD final optionnel (activé automatiquement sur audio dégradé si
     `workflow.vad.auto_enable_final_on_degraded=true`)
   - Nettoyage post-STT (suppression d'artefacts de sous-titrage, fusion de
     micro-segments courts) si `workflow.transcription_cleanup` est actif
   - Correction LLM d'arbitrage (sauf `--skip-llm`) avec `context/session_lexicon_filtered.json`
   - Contrôle qualité → `quality/quality_report.json`
   - Export ZIP

### Enchaînement voix enregistrées

1. Admin ou admin de groupe ouvre `/admin/voices`.
2. Téléchargement du formulaire vierge `/admin/voices/consent-form.pdf`.
3. Création de la voix dans un groupe accessible, avec genre validé.
4. Upload de la preuve signée (`voice_consents`, fichier dans `voices/subjects/<id>/consents/`).
5. Upload d'un audio de référence et génération d'un `voice_profiles.embedding_blob` mocké dans le test.
6. Création d'un job avec `speakers/speaker_clips.json`.
7. `POST /api/jobs/<id>/speakers/voice-match` écrit `speakers/voice_matches.json` et la table `voice_matches`.
8. La page `/jobs/<id>` affiche la suggestion et son genre validé, sans appliquer automatiquement le nom.

---

## Prérequis

Utiliser **impérativement** le venv du projet :

```bash
venv/bin/python tests/test_e2e_workflow.py --help
```

Le test complet nécessite :
- `config.yaml` valide (généré par `scripts/bootstrap_config.py`)
- Cohere ASR disponible dans le venv (ou `--stt-backend whisper`)
- faster-whisper si `--stt-backend whisper`
- pyannote + token HF si diarisation active (mode quality)
- opencode + LLM d'arbitrage OpenAI-compatible si LLM non désactivée
- demucs si `--force-source-separation` ou activation Demucs
- ffmpeg/ffprobe
- GPU NVIDIA pour le pipeline complet

Arrêter le service avant d'exécuter (évite les conflits de port et d'état GPU) :

```bash
systemctl stop transcria
venv/bin/python tests/test_e2e_workflow.py --audio tests/test2.mp3
systemctl start transcria
```

Le parcours E2E applicatif des voix enregistrées ne charge pas de modèle GPU ; il vérifie le flux web et base de données avec une empreinte mockée :

```bash
python -m pytest tests/test_voice_e2e.py -q
```

Le parcours applicatif des lexiques centralisés ne charge pas de modèle GPU :

```bash
python -m pytest tests/test_central_lexicon.py -q
python -m pytest tests/test_workflow_runner.py::TestWorkflowRunnerRunCorrection -q
```

---

## Référence des options

### Audio et job

| Option | Défaut | Description |
|--------|--------|-------------|
| `--audio PATH` | `tests/test1.mp3` | Fichier audio à transcrire (tout format supporté par ffmpeg) |
| `--job-title STR` | `"E2E workflow production"` | Titre du job (utile pour identification en bench) |

L'extension du fichier audio est détectée dynamiquement (`.mp3`, `.m4a`, `.wav`, etc.)
et utilisée pour vérifier l'artefact `input/original<ext>`.

### STT

| Option | Défaut | Description |
|--------|--------|-------------|
| `--stt-backend cohere\|whisper` | `cohere` | Backend de transcription finale |
| `--whisper-model-size SIZE` | `large-v3` | Taille du modèle Whisper si `--stt-backend whisper` |
| `--enable-whisper-lexicon-hotwords` | off | Active l'injection expérimentale des termes de lexique dans les hotwords Whisper |
| `--enable-cohere-lexicon-biasing` | off | Active le biasing contextuel expérimental Cohere par Trie depuis le lexique |
| `--lexicon-term "TERME[|priorité|catégorie|variante1;variante2]"` | aucun | Ajoute un terme au lexique de session du run. Répétable |
| `--lexicon-json PATH` | aucun | Ajoute une liste JSON d'entrées de lexique au run |

> **Backend demandé vs backend effectif** : `--stt-backend` définit le backend
> de départ du run. Le pipeline peut ensuite le remplacer via
> `workflow.quality_transcription.force_stt_backend` si cette règle est explicitement
> activée pour le mode ou selon la décision qualité audio. Par défaut, `--mode quality`
> conserve le backend demandé. Le backend réellement utilisé est écrit dans
> `metadata/transcription_metadata.json["backend"]` et repris dans le JSON de sortie
> E2E sous `effective_stt_backend`.

Quand `--enable-whisper-lexicon-hotwords` est utilisé, l'audit est écrit dans
`metadata/whisper_hotwords.json` et repris dans le JSON de sortie sous
`whisper_hotwords_data`. Cette option n'a d'effet que si le backend effectif est
Whisper. L'audit expose aussi `max_tokens`, `token_count` et
`token_count_method` : TranscrIA compte les tokens avec le tokenizer Whisper
local si disponible, puis bascule sur un fallback approximatif explicitement
tracé si ce tokenizer est absent.

Quand `--enable-cohere-lexicon-biasing` est utilisé, l'audit est écrit dans
`metadata/cohere_lexicon_biasing.json` et repris dans le JSON de sortie sous
`cohere_lexicon_biasing_data`. Cette option n'a d'effet que si le backend
effectif est Cohere.

### Mode pipeline

| Option | Défaut | Description |
|--------|--------|-------------|
| `--mode fast\|quality` | `quality` | `quality` active la diarisation pyannote ; `fast` l'ignore |

### Désactivations

| Option | Description |
|--------|-------------|
| `--skip-llm` | Désactive résumé et correction LLM (STT et diarisation conservés) |
| `--skip-diarization` | Désactive pyannote (pas de locuteurs, pas de genre vocal) |
| `--skip-summary` | Saute la phase résumé entière (avance l'état du job manuellement vers `SUMMARY_DONE`) |

### Prétraitement audio

| Option | Description |
|--------|-------------|
| `--enable-audio-scene` | Force `workflow.audio_scene.enabled=true` |
| `--enable-scene-filter` | Force le filtre scène pré-STT (**implique audio_scene**) |
| `--enable-audio-normalization` | Force la normalisation audio pré-STT |
| `--disable-audio-preflight` | Désactive le pré-diagnostic `metadata/audio_preflight.json` pour baseline |
| `--disable-weak-voice-normalization` | Désactive le profil auto “voix faible/chuchotée” |
| `--enable-audio-denoise` | Active le débruitage expérimental `workflow.audio_denoise.enabled=true` |
| `--force-audio-denoise` | Force le débruitage expérimental (**implique audio_denoise**) |
| `--disable-segment-reliability` | Désactive le score `reliability` par segment ASR |
| `--disable-micro-chunk-merge` | Désactive la fusion conservatrice des micro-tours pyannote |
| `--enable-vad-hysteresis` | Active le mode VAD hystérétique (`workflow.vad.hysteresis_enabled=true`) |
| `--enable-source-separation` | Active le service Demucs (décision soumise aux seuils internes et à `scene_music_min_ratio`) |
| `--force-source-separation` | **Bypass les seuils** — Demucs s'exécute quel que soit l'audio (**implique source-separation**) |

> **Dépendance** : `--enable-scene-filter` nécessite `--enable-audio-scene`.
> Si `--enable-scene-filter` est passé sans `--enable-audio-scene`, le script
> active automatiquement l'analyse de scène (comportement identique au bench runner).

> **Auto-loudnorm** : même sans `--enable-audio-normalization`, le pipeline peut
> forcer une normalisation `loudnorm` si le RMS audio est inférieur à
> `workflow.audio_normalization.auto_loudnorm_rms_threshold` (défaut 0.02).
> Dans ce cas, `metadata/audio_normalization.json` contient `"forced": true`.

> **Pré-diagnostic** : `audio_preflight` est actif par défaut. Il écrit
> `metadata/audio_preflight.json` avec RMS, peak, SNR estimé, bande passante,
> flags et `risk_level`. Le JSON de sortie E2E expose `audio_preflight_data`.

> **Fiabilité segmentaire** : `segment_reliability` est actif par défaut et ajoute
> `reliability=ok|suspect|degrade` aux segments bruts. Le JSON de sortie E2E
> expose `segment_reliability_counts`. Les flags textuels configurables
> (`texte_non_latin`, `hallucination_generique`) ne suppriment pas de segment ;
> ils servent à prioriser la relecture.

> **Métadonnées transcription** : `metadata/transcription_metadata.json` est
> l'artefact de référence pour vérifier ce qui s'est réellement passé côté STT :
> `backend`, `chunking_mode`, `gpu_index`, `language`, `segments`,
> `speaker_count` et `vad_final_enabled`.

> **Débruitage expérimental** : `audio_denoise` reste désactivé par défaut.
> Utiliser `--force-audio-denoise` pour une comparaison A/B contrôlée. La sortie
> attendue est `input/denoised.wav` + `metadata/audio_denoise.json`.

### Campagnes rapides nouvelles features

```bash
# Baseline actuelle avec pré-diagnostic actif
venv/bin/python tests/test_e2e_workflow.py \
  --audio tests/test5.wav --skip-llm --skip-diarization \
  --stt-backend whisper --output-json /tmp/test5_baseline.json

# Débruitage expérimental forcé sur voix chuchotée
venv/bin/python tests/test_e2e_workflow.py \
  --audio tests/test5.wav --skip-llm --skip-diarization \
  --stt-backend whisper --force-audio-denoise \
  --output-json /tmp/test5_denoise.json

# Contrôle sans profil voix faible
venv/bin/python tests/test_e2e_workflow.py \
  --audio tests/test5.wav --skip-llm --skip-diarization \
  --stt-backend whisper --disable-weak-voice-normalization \
  --output-json /tmp/test5_no_weak_voice.json

# Contrôle micro-chunks sur audio long diarisation
venv/bin/python tests/test_e2e_workflow.py \
  --audio tests/test7.mp3 --skip-llm --stt-backend cohere \
  --output-json /tmp/test7_micro_merge_on.json

venv/bin/python tests/test_e2e_workflow.py \
  --audio tests/test7.mp3 --skip-llm --stt-backend cohere \
  --disable-micro-chunk-merge \
  --output-json /tmp/test7_micro_merge_off.json
```

### GPU et LLM

| Option | Défaut | Description |
|--------|--------|-------------|
| `--gpu N` | — | GPU préféré pour le pipeline — positionne `TRANSCRIA_PREFERRED_GPU=N` **avant tout import CUDA/torch** |
| `--arbitrage-port PORT` | config.yaml | Port de la LLM d'arbitrage (utile pour runs parallèles avec plusieurs instances LLM) |

> ⚠️ **`--gpu` utilise `TRANSCRIA_PREFERRED_GPU`, pas `CUDA_VISIBLE_DEVICES`.**
> Le VRAMManager scanne nvidia-smi (qui ignore `CUDA_VISIBLE_DEVICES`) et passe
> des indices physiques à PyTorch. Combiner les deux cause des erreurs
> « invalid device ordinal ». Le script lit `--gpu` depuis `sys.argv` avant tout
> import CUDA et positionne `TRANSCRIA_PREFERRED_GPU`, que `VRAMManager` utilise
> comme GPU de départ préféré avant le fallback sur le meilleur GPU libre.

### Overrides de config

| Option | Description |
|--------|-------------|
| `--config-override CLE=VALEUR` | Override YAML ponctuel, répétable. Ex: `--config-override workflow.vad.enabled_final=true` ou `--config-override whisper.beam_size=7`. Appliqué après les autres flags. |

Les valeurs sont parsées automatiquement : `true/false` → booléen, `0.6` → float,
`7` → int, le reste → chaîne. La notation pointée est utilisée pour les clés
imbriquées (`workflow.vad.threshold`).

### Bench runner

| Option | Description |
|--------|-------------|
| `--combo-id STR` | Identifiant de la combinaison (ex: `023`) — reporté dans `--output-json` |
| `--output-json PATH` | Chemin du JSON de résultats structurés (créé en fin de run pour bench_audio.py) |

En mode bench (`--combo-id` présent), toutes les options audio non demandées sont
**explicitement forcées à OFF** pour neutraliser les valeurs de `config.yaml` de
production (ex: `audio_scene.enabled: true`). Hors bench, seules les options
`--enable-*` activent des options ; les options absentes gardent leur valeur config.

### Gestion du job

| Option | Description |
|--------|-------------|
| `--keep` | Conserve le job à la fin (pour inspecter les SRTs manuellement) |
| `--keep-on-error` | Conserve le job uniquement en cas d'échec (facilite le débogage) |

---

## Exemples

### Run basique

```bash
venv/bin/python tests/test_e2e_workflow.py --audio tests/test2.mp3 --keep
```

### Run rapide sans LLM

```bash
venv/bin/python tests/test_e2e_workflow.py \
  --audio tests/test2.mp3 \
  --skip-llm --keep
```

### Whisper large-v3 avec tout le prétraitement audio

```bash
venv/bin/python tests/test_e2e_workflow.py \
  --audio tests/test2.mp3 \
  --stt-backend whisper --whisper-model-size large-v3 \
  --enable-audio-scene \
  --enable-scene-filter \
  --enable-audio-normalization \
  --force-source-separation \
  --skip-llm --keep
```

### Run bench (appelé par bench_audio.py)

```bash
venv/bin/python tests/test_e2e_workflow.py \
  --audio tests/test1.mp3 \
  --gpu 3 \
  --stt-backend whisper \
  --enable-audio-scene \
  --force-source-separation \
  --enable-audio-normalization \
  --skip-llm \
  --combo-id 023 \
  --output-json /tmp/bench/023.json \
  --keep
```

### Run bench avec config-override (tuning)

```bash
venv/bin/python tests/test_e2e_workflow.py \
  --audio tests/cse_excerpt_10m_15m.wav \
  --gpu 0 \
  --stt-backend whisper \
  --enable-audio-scene \
  --skip-llm \
  --combo-id vad_final \
  --config-override workflow.vad.enabled_final=true \
  --config-override workflow.vad.threshold=0.60 \
  --output-json bench_results/bench.json \
  --keep
```

### Deux runs parallèles avec LLMs dédiées

```bash
# Terminal 1 — pipeline sur GPU 3, LLM arbitrage port 8080
venv/bin/python tests/test_e2e_workflow.py \
  --audio tests/test2.mp3 \
  --gpu 3 --arbitrage-port 8080 \
  --stt-backend cohere \
  --combo-id 001 --output-json /tmp/bench/001.json --keep &

# Terminal 2 — pipeline sur GPU 7, LLM arbitrage2 port 8081
venv/bin/python tests/test_e2e_workflow.py \
  --audio tests/test2.mp3 \
  --gpu 7 --arbitrage-port 8081 \
  --stt-backend whisper \
  --combo-id 005 --output-json /tmp/bench/005.json --keep &

wait
```

### Débogage d'un combo qui échoue

```bash
venv/bin/python tests/test_e2e_workflow.py \
  --audio tests/test2.mp3 \
  --enable-audio-scene \
  --force-source-separation \
  --skip-llm \
  --keep-on-error   # conserve le job même en cas d'échec
```

---

## Format du JSON de sortie (`--output-json`)

```json
{
  "combo_id": "023",
  "run_started_at": "2026-05-22T14:30:00+00:00",
  "audio_file": "test2.mp3",
  "audio_path": "/path/to/test2.mp3",
  "stt_backend": "whisper",
  "effective_stt_backend": "whisper",
  "whisper_model_size": "large-v3",
  "mode": "quality",
  "skip_llm": true,
  "skip_diarization": false,
  "audio_preflight": true,
  "audio_scene": true,
  "source_separation": true,
  "force_source_separation": true,
  "audio_normalization": true,
  "weak_voice_normalization": true,
  "audio_denoise": false,
  "force_audio_denoise": false,
  "segment_reliability": true,
  "micro_chunk_merge": true,
  "vad_hysteresis": false,
  "scene_filter": false,
  "gpu": "3",
  "arbitrage_port": null,
  "config_overrides": {
    "workflow.vad.threshold": 0.6
  },
  "status": "ok",
  "errors": [],
  "timings": {
    "init_s": 1.2,
    "prepare_s": 0.5,
    "summary_s": 45.3,
    "context_s": 0.5,
    "participants_s": 0.2,
    "lexicon_s": 0.1,
    "mapping_s": 0.3,
    "pipeline_s": 87.4,
    "verify_s": 0.4
  },
  "vram_peak_mb": 10240,
  "vram_snapshots": [
    {
      "label": "avant-summary",
      "gpus": [{"id": 0, "mem_used_mb": 19, "mem_free_mb": 24107}]
    }
  ],
  "srt": {
    "raw_segments": 42,
    "raw_words": 387,
    "corrected_exists": false,
    "corrected_words": null,
    "raw_path": "/home/.../jobs/abc123/metadata/transcription.srt",
    "corrected_path": null
  },
  "artifacts": {
    "audio_preflight": true,
    "audio_scene": true,
    "audio_denoise": false,
    "transcription_metadata": true,
    "source_separation": true,
    "scene_filter": false,
    "normalization": true,
    "diarization_checkpoint": true,
    "zip_export": true
  },
  "audio_preflight_data": {
    "risk_level": "ok",
    "flags": [],
    "rms": 0.08,
    "peak": 0.82,
    "estimated_snr_db": 24.5,
    "bandwidth_95_hz": 6200.0,
    "bandwidth_99_hz": 7600.0,
    "silence_ratio": 0.12
  },
  "audio_scene_data": {
    "speech_ratio": 0.85,
    "music_ratio": 0.02,
    "noise_ratio": 0.13,
    "has_music": false,
    "has_noise": false,
    "problem_segments": 2,
    "gender_segments": 7
  },
  "quality_decision": {
    "level": "ok",
    "reasons": []
  },
  "transcription_metadata": {
    "backend": "whisper",
    "chunking_mode": "pyannote_turns",
    "gpu_index": 3,
    "language": "fr",
    "segments": 42,
    "speaker_count": 3,
    "vad_final_enabled": false
  },
  "segment_reliability_counts": {
    "ok": 40,
    "suspect": 2
  },
  "speakers": {
    "count": 3,
    "gender_attributed": 2
  },
  "job_id": "abc123",
  "job_dir": "/home/admin_ia/transcria/jobs/abc123"
}
```

Le champ `config_overrides` est renseigné uniquement si `--config-override` est utilisé.
Les champs `audio_scene_data`, `quality_decision` et `speakers` sont `null` si les
artefacts correspondants n'existent pas. Le champ `vram_snapshots` contient un
snapshot par point de contrôle GPU (initial, avant-summary, après-summary,
avant-pipeline, après-pipeline).

`stt_backend` est la valeur demandée au lancement. `effective_stt_backend` et
`transcription_metadata.backend` sont les valeurs à utiliser pour analyser les
résultats, car elles reflètent le backend réellement utilisé par le pipeline.

---

## Artefacts vérifiés

### Artefacts obligatoires

| Fichier | Condition |
|---------|-----------|
| `input/original.<ext>` | Toujours (extension = celle du fichier audio source) |
| `metadata/audio_analysis.json` | Toujours |
| `summary/quick_transcript.txt` | Si phase résumé active (absent avec `--skip-summary`) |
| `summary/summary.json` | Si phase résumé active (absent avec `--skip-summary`) |
| `summary/summary.md` | Si phase résumé active et LLM active (absent avec `--skip-summary` ou `--skip-llm`) |
| `context/meeting_context.json` | Toujours |
| `context/participants.json` | Toujours |
| `context/session_lexicon.json` | Toujours |
| `context/job_context.yaml` | Toujours |
| `metadata/transcription.srt` | Toujours |
| `metadata/transcription_metadata.json` | Toujours après transcription finale |
| `quality/quality_report.json` | Toujours |
| `speakers/speaker_stats.json` | Si locuteurs détectés |
| `speakers/speaker_mapping.json` | Si locuteurs détectés |
| `metadata/transcription_corrigee.srt` | Si LLM active (absent si `--skip-llm`) |
| `metadata/correction_report.md` | Si LLM active (absent si `--skip-llm`) |
| `exports/*.zip` | Toujours |

### Artefacts optionnels (selon config et pipeline)

| Fichier | Condition |
|---------|-----------|
| `metadata/audio_preflight.json` | Par défaut, sauf `--disable-audio-preflight` |
| `metadata/audio_quality_decision.json` | Toujours (évaluation qualité) |
| `metadata/audio_scene.json` | `--enable-audio-scene` ou config active |
| `metadata/audio_scene_filter.json` | `--enable-scene-filter` |
| `metadata/audio_normalization.json` | `--enable-audio-normalization` ou auto-loudnorm forcé |
| `metadata/audio_denoise.json` | `--enable-audio-denoise` déclenché par seuils, ou `--force-audio-denoise` |
| `input/vocals.wav` | `--force-source-separation` ou Demucs déclenché par seuils |
| `input/scene_filtered.wav` | Filtre scène appliqué |
| `input/normalized.wav` | Normalisation appliquée (manuelle ou auto-loudnorm) |
| `input/denoised.wav` | Débruitage expérimental appliqué |
| `speakers/diarization_checkpoint.json` | Cache pyannote actif |
| `speakers/speaker_embeddings.json` | Embeddings activés |
| `summary/diarization_context.md` | Attribution de genre réussie |

> **Auto-loudnorm** : le pipeline peut forcer une normalisation même sans
> `--enable-audio-normalization` si le RMS audio est inférieur à
> `workflow.audio_normalization.auto_loudnorm_rms_threshold` (défaut 0.02).
> Dans ce cas, `metadata/audio_normalization.json` contient `"forced": true`
> et `input/normalized.wav` est créé.

> **Nettoyage post-STT** : si `workflow.transcription_cleanup` est actif (par
> défaut), le pipeline supprime les artefacts de sous-titrage récurrents
> (`Sous-titrage ST' 501`, `FR 2021`, etc.) et fusionne les micro-segments
> courts d'un même locuteur. Ces opérations sont tracées dans les logs du pipeline
> (`removed_artifacts=N, merged_short_segments=M`).

---

## Post-traitements automatiques du pipeline

Certains traitements s'appliquent automatiquement selon la qualité détectée de l'audio,
indépendamment des flags `--enable-*` :

1.  **Auto-loudnorm** (`pipeline_service.py`) : si le RMS de l'audio est inférieur à
    `auto_loudnorm_rms_threshold` (défaut 0.02) et que la normalisation n'est pas déjà
    active, `loudnorm` est forcé automatiquement. L'artefact `metadata/audio_normalization.json`
    contient alors `"forced": true, "reasons": ["audio_trop_silencieux_auto_loudnorm"]`.

2.  **VAD final automatique** : si `workflow.vad.auto_enable_final_on_degraded=true` (défaut)
    et que la décision qualité est `degrade`, le VAD final est activé avec le seuil
    `workflow.vad.threshold_final_degraded` (défaut 0.6).

3.  **Nettoyage post-STT** (`transcription_cleanup`) : activé par défaut, supprime les
    artefacts de sous-titrage connus et fusionne les micro-segments courts.

4.  **Décision source separation** : même avec `--enable-source-separation`, Demucs ne
    s'exécutera pas si les seuils internes ne sont pas atteints (`scene_music_min_ratio=0.80`,
    `scene_music_min_duration_s=60`). Utiliser `--force-source-separation` pour bypasser.

---

## Intégration avec bench_audio.py

Le test est conçu pour être appelé en sous-processus par `scripts/bench_audio.py` :

```python
subprocess.run([
    "venv/bin/python", "tests/test_e2e_workflow.py",
    "--audio", str(audio_path),
    "--gpu", str(gpu_id),
    "--stt-backend", combo["stt_backend"],
    "--combo-id", combo["id"],
    "--output-json", str(output_json_path),
    "--skip-llm",
    "--keep",
    # + flags audio selon le combo
], check=False)
```

Le bench runner supporte aussi `--config-override` pour les runs de tuning :

```bash
venv/bin/python scripts/bench_audio.py \
    --audio tests/cse_excerpt_10m_15m.wav \
    --combos 013 \
    --gpu-pool 0,1,2 \
    --workers 3 \
    --config-override workflow.vad.enabled_final=true \
    --config-override workflow.vad.threshold=0.60 \
    --output-dir bench_results_cse_excerpt_vad060
```

Le JSON produit par `--output-json` est ensuite agrégé par le bench runner pour
générer le `summary.csv` comparatif.

---

## Dépendance scene_filter → audio_scene

`--enable-scene-filter` **requiert** `--enable-audio-scene`.
Si `--enable-scene-filter` est passé sans `--enable-audio-scene`, le script
active automatiquement l'analyse de scène (comportement identique au bench runner).
Cela explique pourquoi la matrice benchmark compte 24 combos et non 32 :
les 8 combos où `filter=1` et `scene=0` sont impossibles.

## Force source separation

`--force-source-separation` positionne `workflow.source_separation.force=true` dans la
config, ce qui bypasse le `SourceSeparationDecider` dans `pipeline_service.py`.
Demucs s'exécute alors **quel que soit le contenu audio** (musique, bruit ou voix seule).
Nécessite que Demucs soit installé : `venv/bin/pip install demucs`.

## Mode bench vs mode manuel

En mode bench (`--combo-id` présent), le comportement diffère sur un point critique :
toutes les options audio non demandées sont **explicitement forcées à OFF** pour
neutraliser les valeurs de `config.yaml` de production. Par exemple, si `config.yaml`
contient `workflow.audio_scene.enabled: true` et que le combo n'inclut pas l'analyse
de scène, le script force `audio_scene.enabled=false`.

En mode manuel (sans `--combo-id`), seules les options `--enable-*` activent des
traitements ; les options absentes gardent leur valeur dans `config.yaml`.

## GPU et parallélisme

Le script positionne `TRANSCRIA_PREFERRED_GPU` (et non `CUDA_VISIBLE_DEVICES`) pour
éviter les conflits avec nvidia-smi utilisé par `VRAMManager`. Pour lancer plusieurs
pipelines en parallèle avec `bench_audio.py`, spécifier `--gpu-pool` avec des GPUs
distincts : chaque worker reçoit son propre `--gpu` qui devient
`TRANSCRIA_PREFERRED_GPU` dans le sous-processus.

En mode `--skip-llm`, le service TranscrIA n'a pas besoin d'être arrêté si les
GPUs utilisés ne chevauchent pas ceux de la LLM d'arbitrage (GPU 0-2 par défaut).

# Test E2E TranscrIA

## Objectif

`tests/test_e2e_workflow.py` exécute un job réel avec le même enchaînement que le workflow applicatif :

1. `JobService.create/upload/analyze`
2. `WorkflowRunner.run_summary()` : transcription rapide, pyannote, résumé LLM si activé
   - pyannote écrit `speaker_turns.json` ; `_inject_speaker_genders` attribue acoustiquement un genre à chaque SPEAKER_XX dans `speaker_stats.json` (si `audio_scene.json` disponible à ce stade).
3. validation du contexte, des participants et du lexique via les managers applicatifs
4. mapping des `SPEAKER_XX` sans noms humains injectés
5. `PipelineService.run_process(..., mode="quality")` :
   - analyse de scène audio → `metadata/audio_scene.json` avec ratios, `scene_segments`, `problem_segments` et `gender_segments`
   - séparation de sources optionnelle (Demucs) → `input/vocals.wav` si appliquée
   - filtrage scène optionnel → `input/scene_filtered.wav` + `metadata/audio_scene_filter.json`
   - normalisation audio optionnelle → `input/normalized.wav` + `metadata/audio_normalization.json`
   - transcription finale (Cohere ou Whisper qualité)
   - diarisation pyannote → `speaker_turns.json` → `_inject_speaker_genders` attribue le genre (avec `audio_scene.json` déjà disponible)
   - correction LLM d'arbitrage
   - contrôle qualité
   - export ZIP

Le test ne préremplit plus de participants fictifs. Il crée une entrée par locuteur détecté et laisse la LLM remplir les rôles ou noms si elle les déduit.

## Prérequis

Utiliser impérativement le venv du projet :

```bash
venv/bin/python tests/test_e2e_workflow.py --help
```

Le test complet nécessite :
- un `config.yaml` valide ;
- Cohere ASR disponible dans le venv ;
- faster-whisper disponible si `--stt-backend whisper` ou mode qualité ;
- pyannote disponible si la diarisation est activée ;
- opencode et la LLM d'arbitrage OpenAI-compatible configurés si le LLM n'est pas sauté ;
- ffmpeg/ffprobe ;
- GPU NVIDIA pour le pipeline complet.

Les prétraitements `audio_scene_filter` et `audio_normalization` préservent la timeline :
ils ne coupent pas l'audio. Les métadonnées associées doivent contenir
`preserve_timeline=true`.

## Commandes

Run complet sur l'audio par défaut :

```bash
venv/bin/python tests/test_e2e_workflow.py --keep
```

Run complet sur `test2.mp3` :

```bash
venv/bin/python tests/test_e2e_workflow.py --audio tests/test2.mp3 --keep
```

Run plus rapide sans LLM :

```bash
venv/bin/python tests/test_e2e_workflow.py --audio tests/test2.mp3 --skip-llm
```

Run avec les prétraitements audio optionnels forcés :

```bash
venv/bin/python tests/test_e2e_workflow.py \
  --audio tests/test2.mp3 \
  --skip-llm \
  --enable-audio-scene \
  --enable-scene-filter \
  --enable-audio-normalization \
  --keep
```

Run avec séparation de sources forcée (nécessite Demucs fonctionnel ; la décision
reste soumise aux seuils du pipeline) :

```bash
venv/bin/python tests/test_e2e_workflow.py \
  --audio tests/test2.mp3 \
  --enable-audio-scene \
  --enable-source-separation \
  --keep
```

Options utiles :

```bash
--audio PATH              fichier audio à utiliser
--keep                    conserve le job pour inspection
--skip-llm                désactive résumé/correction LLM
--skip-diarization        désactive pyannote
--stt-backend cohere      backend STT par défaut
--stt-backend whisper     test avec faster-whisper
--enable-audio-scene          force l'analyse de scène audio
--enable-scene-filter         force le filtre scène pré-STT (désactivé par défaut)
--enable-audio-normalization  force la normalisation pré-STT (désactivée par défaut)
--enable-source-separation    force l'activation du service de séparation (si seuils atteints)
```

## Artefacts vérifiés

Le test contrôle notamment :
- `metadata/audio_analysis.json`
- `metadata/audio_quality_decision.json` si décision qualité écrite
- `metadata/audio_scene.json` (optionnel — ratios, `scene_segments`, `problem_segments`, `gender_segments`)
- `metadata/audio_scene_filter.json` si filtrage scène appliqué (`preserve_timeline=true`)
- `metadata/audio_normalization.json` si normalisation appliquée (`preserve_timeline=true`)
- `input/vocals.wav` si séparation de sources appliquée
- `input/scene_filtered.wav` si filtrage scène appliqué
- `input/normalized.wav` si normalisation appliquée
- `summary/quick_transcript.txt`
- `summary/summary.json`
- `summary/summary.md`
- `summary/diarization_context.md` (optionnel — contient section "Genre vocal par locuteur" si attribution réussie)
- `context/meeting_context.json`
- `context/participants.json`
- `context/session_lexicon.json`
- `context/job_context.yaml`
- `speakers/speaker_stats.json` (champ `gender` pré-rempli par attribution acoustique si possible)
- `speakers/diarization_checkpoint.json` si cache pyannote actif
- `speakers/speaker_embeddings.json` si checkpoint embeddings actif
- `speakers/speaker_mapping.json`
- `metadata/transcription.srt`
- `metadata/transcription_corrigee.srt` si LLM activé
- `metadata/correction_report.md` si LLM activé
- `quality/quality_report.json`
- `exports/*.zip`

La section **Genre vocal par locuteur** affiche en fin de test le résultat de l'attribution automatique (`female`/`male`/non attribué) pour chaque SPEAKER_XX avec son nom mappé.

Avec `--keep`, le job est conservé dans `jobs/<job_id>/` pour inspection manuelle.

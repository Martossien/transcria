# Test E2E TranscrIA

## Objectif

`tests/test_e2e_workflow.py` exécute un job réel avec le même enchaînement que le workflow applicatif :

1. `JobService.create/upload/analyze`
2. `WorkflowRunner.run_summary()` : transcription rapide, pyannote, résumé LLM si activé
   - pyannote écrit `speaker_turns.json` ; `_inject_speaker_genders` attribue acoustiquement un genre à chaque SPEAKER_XX dans `speaker_stats.json` (si `audio_scene.json` disponible à ce stade).
3. validation du contexte, des participants et du lexique via les managers applicatifs
4. mapping des `SPEAKER_XX` sans noms humains injectés
5. `PipelineService.run_process(..., mode="quality")` :
   - analyse de scène audio → `metadata/audio_scene.json` avec `gender_segments` horodatés
   - séparation de sources optionnelle (Demucs)
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

Options utiles :

```bash
--audio PATH              fichier audio à utiliser
--keep                    conserve le job pour inspection
--skip-llm                désactive résumé/correction LLM
--skip-diarization        désactive pyannote
--stt-backend cohere      backend STT par défaut
--stt-backend whisper     test avec faster-whisper
```

## Artefacts vérifiés

Le test contrôle notamment :
- `metadata/audio_analysis.json`
- `metadata/audio_quality_decision.json` si décision qualité écrite
- `metadata/audio_scene.json` (optionnel — contient `gender_segments` si `detect_gender=true`)
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

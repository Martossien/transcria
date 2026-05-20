# Test E2E TranscrIA

## Objectif

`tests/test_e2e_workflow.py` exécute un job réel avec le même enchaînement que le workflow applicatif :

1. `JobService.create/upload/analyze`
2. `WorkflowRunner.run_summary()` : transcription rapide, pyannote, résumé LLM si activé
3. validation du contexte, des participants et du lexique via les managers applicatifs
4. mapping des `SPEAKER_XX` sans noms humains injectés
5. `PipelineService.run_process(..., mode="quality")` : transcription finale, correction LLM, qualité, export

Le test ne préremplit plus de participants fictifs. Il crée une entrée par locuteur détecté et laisse la LLM remplir les rôles ou noms si elle les déduit.

## Prérequis

Utiliser impérativement le venv du projet :

```bash
venv/bin/python tests/test_e2e_workflow.py --help
```

Le test complet nécessite :
- un `config.yaml` valide ;
- Cohere ASR disponible dans le venv ;
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
- `summary/quick_transcript.txt`
- `summary/summary.json`
- `summary/summary.md`
- `context/meeting_context.json`
- `context/participants.json`
- `context/session_lexicon.json`
- `context/job_context.yaml`
- `speakers/speaker_stats.json`
- `speakers/speaker_mapping.json`
- `metadata/transcription.srt`
- `metadata/transcription_corrigee.srt` si LLM activé
- `metadata/correction_report.md` si LLM activé
- `quality/quality_report.json`
- `exports/*.zip`

Avec `--keep`, le job est conservé dans `jobs/<job_id>/` pour inspection manuelle.

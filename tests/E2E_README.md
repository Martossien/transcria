# Test E2E TranscrIA — Guide d'utilisation

## Description

Le test E2E (`test_e2e_workflow.py`) exécute le pipeline complet TranscrIA sur un fichier audio réel avec vérification de l'activité GPU à chaque étape critique :

- **Cohere ASR** : transcription sur GPU (vérifie l'allocation VRAM et le processus GPU)
- **Qwen 35B résumé** : LLM via llama.cpp (vérifie ~50 Go sur 3 GPUs)
- **pyannote** : diarisation sur GPU (vérifie le chargement du modèle)
- **Cohere ASR #2** : seconde passe avec locuteurs (vérifie le processus GPU)
- **Qwen 35B correction** : LLM via llama.cpp (vérifie la VRAM)

Le script capture `nvidia-smi` avant/après chaque étape GPU et produit une timeline VRAM complète.

## Prérequis

### Environnement Python

Utiliser le **venv du projet** (pas le conda `transcript`, pas le Python système) :

```bash
# Activer le venv
source venv/bin/activate

# Vérifier que tous les composants sont disponibles
python -c "
import torch; print('torch:', torch.__version__, 'CUDA:', torch.cuda.is_available())
import transformers; print('transformers:', transformers.__version__)
from pyannote.audio import Pipeline; print('pyannote: OK')
from transcria.stt.cohere_transcriber import CohereTranscriber
print('Cohere available:', CohereTranscriber().available)
from transcria.stt.diarization import DiarizerService
print('Diarizer available:', DiarizerService({}).available)
"
```

Sortie attendue :
```
torch: 2.12.0+cu130 CUDA: True 8
transformers: 4.57.6
pyannote: OK
Cohere available: True
Diarizer available: True
```

> **Important** : Le conda `transcript` utilise GraalPy et ne fonctionne PAS pour ce test. Toujours utiliser le venv du projet.

### Dépendances dans le venv

Si le venv n'est pas encore configuré :

```bash
cd /chemin/vers/transcria
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install accelerate
```

### Matériel

- **GPU** : Au moins 1 GPU NVIDIA avec 24 Go VRAM (le test complet nécessite ~50 Go sur 3 GPUs pour Qwen 35B)
- **FFmpeg/ffprobe** : installé sur le système (`apt install ffmpeg`)
- **opencode CLI** : configuré pour le modèle Qwen 35B local (voir `docs/INSTALL.md`)
- **config.yaml** : fichier de configuration présent à la racine du projet

### Fichier audio de test

Le test utilise `tests/test1.mp3` (291 Ko, 29.2s, voix française). Si absent, le script signale l'erreur.

## Utilisation

### Lancement complet

```bash
cd /chemin/vers/transcria
source venv/bin/activate
python tests/test_e2e_workflow.py
```

### Options

```bash
# Conserver le job après le test (ne pas supprimer le répertoire jobs/<id>)
python tests/test_e2e_workflow.py --keep

# Sauter les étapes LLM (Qwen résumé + correction) — plus rapide, pas de GPU requis
python tests/test_e2e_workflow.py --skip-llm

# Sauter la diarisation pyannote
python tests/test_e2e_workflow.py --skip-diarization

# Combiner les options
python tests/test_e2e_workflow.py --keep --skip-llm
```

### Durée estimée

| Mode | Durée | GPU requis |
|---|---|---|
| Complet | ~3-4 min | 3 GPUs (50 Go VRAM pour Qwen) |
| `--skip-llm` | ~30s | 1 GPU (6 Go pour Cohere + pyannote) |
| `--skip-llm --skip-diarization` | ~5s | 1 GPU (6 Go pour Cohere) |

## Résultats attendus

### Sortie console

Le script affiche :
1. L'état de chaque GPU à chaque étape critique (VRAM utilisée/libre, utilisation GPU, processus)
2. Les deltas VRAM avant/après chaque modèle GPU
3. Une timeline GPU complète en fin de test
4. Les temps par étape
5. Le nombre d'étapes réussies/échouées/ignorées

Exemple de timeline GPU :
```
Étape                                     GPU0 Used  GPU0 Free  Procs
──────────────────────────────────────────────────────────────────────
État initial                                  16 Mo   24109 Mo      0
APRÈS Cohere ASR (offload)                   388 Mo   23737 Mo      1
Qwen 35B chargé (résumé)                   19063 Mo    5062 Mo      9
APRÈS pyannote (offload)                   12700 Mo   11425 Mo      1
AVANT Qwen 35B (correction)                  400 Mo   23725 Mo      1
APRÈS Qwen 35B (correction, arrêt)         14253 Mo    9872 Mo      9
```

### Résultat final

```
Résultats : 14 réussis / 0 échoués / 0 ignorés / 15 total
🎉 Tous les tests E2E sont passés !
```

| Étape | Description | Vérification GPU |
|---|---|---|
| 1. Initialisation | Flask + DB + admin | — |
| 2. Création du job | Job en base | — |
| 3. Upload audio | Copie fichier | — |
| 4. Analyse audio | ffprobe | — |
| 5. Conversion WAV | ffmpeg | — |
| 6. Cohere ASR | Transcription sur GPU | Processus python + delta VRAM |
| 7. Résumé LLM | Qwen 35B via opencode | Processus llama-server + delta VRAM |
| 8. Diarisation | pyannote sur GPU | Processus python + delta VRAM |
| 9. Contexte | MeetingContext | — |
| 10. Participants | ParticipantsManager | — |
| 11. Lexique | LexiconManager | — |
| 12. Mapping locuteurs | SpeakerDetector | — |
| 13. Traitement | Cohere #2 + qualité + ZIP | Processus python + delta VRAM |
| 14. Correction SRT | Qwen 35B via opencode | Processus llama-server + delta VRAM |
| 15. Vérification fichiers | 9 fichiers attendus | — |

## Points de contrôle GPU

Le script `verify_gpu_activity()` vérifie après chaque étape GPU :
- **Delta VRAM** : différence de VRAM utilisée sur chaque GPU avant/après
- **Processus GPU** : présence de processus CUDA attendus (python pour Cohere/pyannote, llama-server pour Qwen)
- **Seuils** : Cohere ≥ 500 Mo, Qwen ≥ 1000 Mo (les deltas faibles sont signalés mais ne font pas échouer le test)

## Fichiers produits

Avec `--keep`, le répertoire `jobs/<id>/` est conservé avec :
```
jobs/<id>/
  input/           audio_converted.wav, original.mp3
  metadata/         audio_analysis.json, speakers_map.json, transcription*.srt
  summary/          summary.md, summary.json, quick_transcript.txt
  context/          job_context.yaml, participants.json, session_lexicon.json
  speakers/         speaker_turns.json, speaker_stats.json, speaker_clips.json
  quality/          quality_report.json, quality_report.md, review_points.json
  exports/          transcrIA_job_<id>.zip
```

## Dépannage

### « Cohere ASR non disponible »

Vérifier que le venv est activé et que `accelerate` est installé :
```bash
source venv/bin/activate
pip install accelerate
python -c "from transcria.stt.cohere_transcriber import CohereTranscriber; print(CohereTranscriber().available)"
# Doit afficher : True
```

### « pyannote non disponible »

Vérifier que le venv a `pyannote.audio` :
```bash
source venv/bin/activate
pip install pyannote.audio
python -c "from transcria.stt.diarization import DiarizerService; print(DiarizerService({}).available)"
# Doit afficher : True
```

### Erreur torchcodec / FFmpeg

Si `torchaudio.load()` échoue avec une erreur `torchcodec` :
- Le module torchaudio fallback sur soundfile/librosa devrait fonctionner
- Pour corriger complètement : installer ffmpeg dans le venv (`apt install ffmpeg` ou `apt install ffmpeg`)

### « opencode introuvable »

Configurer le binaire opencode (voir `docs/INSTALL.md`, section 4) :
```bash
export TRANSCRIA_OPENCODE_BIN=~/.opencode/bin/opencode
```

### GPUs non détectés

Vérifier les pilotes NVIDIA :
```bash
nvidia-smi
# Doit afficher les GPUs avec CUDA 12.x
```

## Tests unitaires

Les tests unitaires (379 tests) s'exécutent avec pytest :

```bash
source venv/bin/activate
python -m pytest tests/ -q
# Résultat attendu : 379 passed
```

Ces tests mock les appels GPU et ne nécessitent pas de vrai matériel.
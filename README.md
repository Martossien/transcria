# TranscrIA MVP

**Portail de transcription et valorisation de réunions** — Transformez un enregistrement audio/vidéo en livrables exploitables : transcription SRT horodatée, résumé structuré, identification des locuteurs, lexique métier, rapport qualité et package ZIP final.

---

## Fonctionnalités

- **Transcription ASR** avec Cohere Transcribe (modèle 2B, Fast-Conformer)
- **Diarisation** avec pyannote community-1 (11+ locuteurs, extraits audio par locuteur)
- **Résumé LLM** via Qwen 3.6 35B (opencode CLI, 263K contexte)
- **Correction SRT** par LLM (orthographe, lexique métier, attribution locuteurs)
- **Contrôle qualité** automatisé (9 checks, score /100)
- **Export ZIP** complet (SRT, contexte, participants, lexique, rapport qualité)
- **Interface web** guidée en 9 étapes (wizard Flask)
- **Authentification** et rôles (admin, manager, operator, viewer)
- **Configuration** en YAML avec interface admin
- **Cycle GPU** orchestré (Cohere → pyannote → Qwen 35B sur 2× RTX 5090)

## Stack technique

| Composant | Technologie |
|---|---|
| Backend | Python 3.11+, Flask 3.x, SQLAlchemy, SQLite |
| Frontend | Jinja2, Bootstrap 5 |
| ASR | Cohere Transcribe 03-2026 (Fast-Conformer, bfloat16) |
| Diarisation | pyannote community-1 |
| LLM résumé/correction | Qwen 3.6 35B via opencode CLI (llama.cpp ou vLLM) |
| Monitoring GPU | Dashboard LLM (port 5001) |
| Éditeur SRT | SRT Editor EASY (port 7861, optionnel) |

## Prérequis

- Python 3.11+
- GPU(s) NVIDIA avec CUDA 12.x (1× GPU 16 Go minimum, 2× GPU 24 Go pour le cycle complet avec Qwen 35B)
- ffmpeg / ffprobe (binaires système, `apt install ffmpeg`)
- opencode CLI (pour le moteur LLM, voir [INSTALL.md](docs/INSTALL.md))
- Un modèle LLM compatible OpenAI API sur le port 8080

## Installation

```bash
git clone https://github.com/Martossien/transcria.git
cd transcria
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu126
pip install -r requirements.txt
pip install accelerate
```

> **Important** : PyTorch doit être installé **avec CUDA** via `--index-url`. L'installation pip classique installe la version CPU-only. Voir [docs/INSTALL.md](docs/INSTALL.md) pour le guide complet (création du venv, téléchargement des modèles, configuration, dépannage).

### Configuration

```bash
cp config.example.yaml config.yaml
# Éditer config.yaml avec vos chemins et paramètres
```

Variables d'environnement :

| Variable | Description | Défaut |
|---|---|---|
| `TRANSCRIA_CONFIG` | Chemin vers le fichier config.yaml | `config.yaml` |
| `TRANSCRIA_SECRET` | Clé secrète Flask sessions | Aléatoire |
| `TRANSCRIA_PORT` | Port du serveur | `7870` |
| `TRANSCRIA_HOST` | Hôte d'écoute | `0.0.0.0` |
| `TRANSCRIA_DEBUG` | Mode debug | `false` |
| `HF_TOKEN` | Token HuggingFace (pour pyannote) | — |
| `TRANSCRIA_ARBITRAGE_SCRIPT` | Script de lancement LLM | `./scripts/launch_arbitrage.sh` |
| `TRANSCRIA_STOP_SCRIPT` | Script d'arrêt LLM | `./scripts/stop_qwen.sh` |
| `TRANSCRIA_OPENCODE_BIN` | Chemin vers opencode | `opencode` (dans le PATH) |

### Initialisation

Au premier lancement, un utilisateur `admin` est créé avec le mot de passe défini dans `config.yaml` (`auth.first_admin_password`).

## Utilisation

```bash
# Démarrage
./start.sh --port 7870

# Arrêt
./stop.sh

# Statut
./status.sh
```

Ouvrir `http://localhost:7870` dans un navigateur.

### Workflow

1. **Upload** — Dépôt du fichier audio/vidéo (mp3, wav, m4a, mp4, flac, ogg)
2. **Analyse** — Analyse ffprobe (durée, format, bitrate)
3. **Résumé** — Transcription Cohere + résumé structure Qwen 35B + diarisation pyannote
4. **Contexte** — Validation/édition des suggestions (titre, type, sujet, objectif)
5. **Participants** — Identification des locuteurs avec extraits audio
6. **Lexique** — Validation des termes suspects détectés par le LLM
7. **Traitement** — Transcription + diarisation + correction LLM
8. **Qualité** — Score automatique sur 9 critères
9. **Export** — Package ZIP téléchargeable

## Tests

```bash
source venv/bin/activate

# Tests unitaires (385 tests, mock, pas de GPU requis)
python -m pytest tests/ -q

# Test E2E complet (nécessite les GPUs, voir tests/E2E_README.md)
python tests/test_e2e_workflow.py

# Test E2E sans LLM (plus rapide, 1 GPU suffit)
python tests/test_e2e_workflow.py --skip-llm
```

## Structure du projet

```
transcria-mvp/
├── app.py                          # Point d'entrée Flask
├── config.example.yaml             # Template de configuration
├── requirements.txt                # Dépendances Python
├── transcria/
│   ├── config.py                   # Singleton config YAML
│   ├── database.py                 # SQLAlchemy instance
│   ├── auth/                       # Users, Roles, Permissions
│   ├── jobs/                       # Job model (20 états), JobStore, JobFilesystem
│   ├── workflow/                   # WorkflowRunner, WorkflowState, WorkflowSteps
│   ├── audio/                      # AudioAnalyzer (ffprobe), AudioConverter (ffmpeg)
│   ├── stt/                        # CohereTranscriber, DiarizerService, SpeakerDetector
│   ├── context/                    # MeetingContext, Participants, Lexicon, JobContextBuilder
│   ├── quality/                    # QualityReporter (9 checks, score /100)
│   ├── exports/                    # PackageBuilder (ZIP)
│   ├── gpu/                        # VRAMManager, OpenCodeRunner
│   └── web/                        # Routes Flask + templates Jinja2
├── configs/prompts/                # Prompts LLM (summary, correction, arbitration)
├── tests/                          # 22 fichiers pytest + E2E workflow
└── docs/                           # Documentation technique
```

## Documentation

| Document | Contenu |
|---|---|
| [INSTALL.md](docs/INSTALL.md) | Guide d'installation complet (venv, modèles, config, dépannage) |
| [TECHNICAL.md](docs/TECHNICAL.md) | Architecture détaillée, flux de données, API REST, pipeline GPU |
| [BUGS.md](docs/BUGS.md) | 15 bugs documentés avec causes et corrections |
| [DATA_MODEL.md](docs/DATA_MODEL.md) | Schéma de données, états, transitions, arborescence disque |
| [CONFIG_REFERENCE.md](docs/CONFIG_REFERENCE.md) | Référence complète des paramètres config.yaml |
| [ANALYSIS_COHERE_ASR_OPTIMIZATION.md](docs/ANALYSIS_COHERE_ASR_OPTIMIZATION.md) | Analyse d'optimisation du pipeline ASR |
| [ANALYSIS_COHERE_PYANNOTE_CHUNKING.md](docs/ANALYSIS_COHERE_PYANNOTE_CHUNKING.md) | Analyse du chunking pyannote vs chunks 30s |
| [PRESENTATION_UTILISATEUR_DIRECTION.md](docs/PRESENTATION_UTILISATEUR_DIRECTION.md) | Présentation utilisateur et direction |

## Licence

Ce projet est distribué sous la licence [GNU Affero General Public License v3.0](LICENSE).
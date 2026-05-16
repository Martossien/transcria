# TranscrIA

**Service de transcription et valorisation de réunions** — TranscrIA transforme un enregistrement audio/vidéo long en livrables exploitables : transcription SRT horodatée, résumé structuré, identification des locuteurs, lexique métier, rapport qualité et package ZIP final.

---

## Fonctionnalités

- **Transcription ASR** — Cohere Transcribe (2B, Fast-Conformer) ou Whisper large-v3 (via faster-whisper), sélectionnable en config
- **Diarisation** avec pyannote community-1 (extraits audio par locuteur)
- **Résumé LLM** via Qwen 3.6 35B (opencode CLI, 263K contexte)
- **Correction SRT** par LLM (orthographe, lexique métier, attribution locuteurs)
- **Contrôle qualité** automatisé (9 checks, score /100, seuils configurables)
- **Export ZIP** complet (SRT, contexte, participants, lexique, rapport qualité)
- **Interface web** guidée en 9 étapes (wizard Flask + Bootstrap 5)
- **Worker interne** sérialisé pour exécuter les traitements longs hors requête HTTP
- **Authentification** et rôles (admin, manager, operator, viewer)
- **Configuration** YAML avec validation, détection automatique de l'environnement et interface admin
- **Cycle GPU** orchestré (STT → pyannote → Qwen 35B), context manager GPUSession

## Stack technique

| Composant | Technologie |
|---|---|
| Backend | Python 3.11+, Flask 3.x, SQLAlchemy, SQLite |
| Frontend | Jinja2, Bootstrap 5, JavaScript vanilla |
| ASR | Cohere Transcribe 03-2026 ou Whisper (faster-whisper, large-v3 par défaut) |
| Diarisation | pyannote community-1 |
| LLM | Qwen 3.6 35B via opencode CLI — backends supportés : script (llama.cpp), Ollama, HTTP |
| GPU | VRAMManager, GPUSession (context manager), LLMBackend (script/ollama/http) |
| Monitoring | Dashboard LLM (optionnel, port 5001) |
| Éditeur SRT | SRT Editor EASY (optionnel, port 7861) |

## Prérequis

- Python 3.11+
- GPU(s) NVIDIA avec CUDA 12.x :
  - 1 GPU 16 Go minimum pour le pipeline ASR seul
  - 2+ GPUs 24 Go recommandés pour le cycle complet avec Qwen 35B (~48 Go VRAM)
- ffmpeg / ffprobe (`apt install ffmpeg`)
- opencode CLI (pour le moteur LLM)
- Les modèles IA pré-téléchargés (l'application fonctionne en mode offline)

## Installation rapide

```bash
git clone https://github.com/Martossien/transcria.git
cd transcria
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu126
pip install -r requirements.txt
pip install accelerate
python scripts/bootstrap_config.py --output config.yaml
```

> Guide complet (modèles, configuration, dépannage, service systemd) : **[docs/INSTALL.md](docs/INSTALL.md)**

### Configuration

Le bootstrap ci-dessus remplit automatiquement ce qu’il peut détecter.
Vérifier ensuite `config.yaml`, en particulier :
- le mot de passe admin initial
- les chemins de modèles réellement installés
- les scripts et endpoints LLM si votre environnement diffère du template

L'interface d'administration (`/admin/config`) valide la configuration et détecte automatiquement l'environnement (GPUs, binaires, RAM).

Variables d'environnement (optionnelles, surchargent `config.yaml`) :

| Variable | Description | Défaut |
|---|---|---|
| `TRANSCRIA_CONFIG` | Chemin vers config.yaml | `config.yaml` |
| `TRANSCRIA_SECRET` | Clé secrète Flask | Aléatoire |
| `TRANSCRIA_PORT` | Port du serveur | `7870` |
| `TRANSCRIA_HOST` | Hôte d'écoute | `0.0.0.0` |
| `TRANSCRIA_DEBUG` | Mode debug | `false` |
| `HF_TOKEN` | Token HuggingFace (pyannote) | — |
| `TRANSCRIA_OPENCODE_BIN` | Chemin opencode | `opencode` |

## Utilisation

```bash
./start.sh --port 7870    # Démarrer
./stop.sh                  # Arrêter
./status.sh                # Statut
```

Ouvrir `http://localhost:7870`. Au premier lancement, l'utilisateur `admin` est créé avec le mot de passe défini dans `config.yaml`.

### Supervision

```text
GET /health   -> état du process + base SQLite
GET /ready    -> service prêt à accepter des jobs
GET /metrics  -> métriques Prometheus légères
```

### Workflow (9 étapes)

1. **Fichier** — Dépôt du fichier audio/vidéo
2. **Analyse** — ffprobe (durée, codec, canaux, fréquence)
3. **Résumé** — Transcription STT + résumé LLM + diarisation pyannote
4. **Contexte** — Suggestions LLM pré-remplies (titre, type, sujet, objectif)
5. **Participants** — Locuteurs détectés avec extraits audio écoutables
6. **Lexique** — Termes métier (import .txt/.csv, catégories, priorités)
7. **Traitement** — Transcription finale + correction LLM (orthographe, locuteurs, lexique)
8. **Qualité** — Score /100 sur 9 critères, points de relecture
9. **Export** — Package ZIP (SRT corrigé, contexte, participants, lexique, rapports)

## Tests

```bash
source venv/bin/activate
python -m pytest tests/ -q                          # 412 tests unitaires (mock, pas de GPU)
python tests/test_e2e_workflow.py --skip-llm        # E2E rapide (1 GPU)
python tests/test_e2e_workflow.py                   # E2E complet (GPUs + LLM requis)
python tests/test_e2e_workflow.py --stt-backend whisper  # Avec Whisper large-v3
```

## Structure du projet

```
transcria/
├── app.py                       # Point d'entrée Flask
├── config.yaml                  # Configuration (non versionné)
├── config.example.yaml          # Template de configuration
├── requirements.txt             # Dépendances
├── transcria/
│   ├── config/                  # Chargement YAML, validation, détection système
│   ├── database.py              # Instance SQLAlchemy
│   ├── logging_setup.py         # Logger structuré (correlation_id, contexte)
│   ├── auth/                    # Utilisateurs, rôles, permissions, routes /login
│   ├── jobs/                    # Modèle Job (20 états), CRUD, filesystem
│   ├── workflow/                # Étapes (9), calcul d'état, runner
│   ├── audio/                   # Analyse (ffprobe), conversion (ffmpeg)
│   ├── stt/                     # BaseTranscriber (ABC), CohereTranscriber, WhisperTranscriber
│   │                            #   Transcriber, DiarizerService, SpeakerDetector, SummaryGenerator
│   │                            #   TranscriberFactory
│   ├── context/                 # Contexte réunion, participants, lexique
│   ├── quality/                 # 9 checks qualité, score /100, seuils configurables
│   ├── exports/                 # PackageBuilder (ZIP)
│   ├── integrations/            # DashboardClient, SrtEditorLink
│   ├── gpu/                     # VRAMManager, GPUSession, OpenCodeRunner, LLMBackend
│   ├── services/                # Service layer (JobService, PipelineService, ConfigService, JobExecutorService)
│   └── web/                     # Routes Flask + templates Jinja2 + JS
├── scripts/                     # Scripts shell + bootstrap_config.py
├── configs/
│   ├── prompts/                 # Prompts LLM (summary, correction)
│   └── lexique_metier.txt       # Lexique métier global
├── tests/                       # 20+ modules pytest + test E2E
└── docs/                        # Documentation
```

## Documentation

| Document | Contenu |
|---|---|
| [INSTALL.md](docs/INSTALL.md) | Guide complet : venv, modèles, config, service systemd, dépannage |
| [TECHNICAL.md](docs/TECHNICAL.md) | Architecture, flux de données, API REST, pipeline GPU |
| [DATA_MODEL.md](docs/DATA_MODEL.md) | États, transitions, arborescence disque par job |
| [CONFIG_REFERENCE.md](docs/CONFIG_REFERENCE.md) | Référence complète des paramètres config.yaml |
| [PRESENTATION_UTILISATEUR_DIRECTION.md](docs/PRESENTATION_UTILISATEUR_DIRECTION.md) | Présentation utilisateur et direction |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Guide de contribution, architecture, conventions |
| [SECURITY.md](SECURITY.md) | Politique de sécurité et signalement |
| [CHANGELOG.md](CHANGELOG.md) | Historique des évolutions notables |

## Licence

Ce projet est distribué sous la licence [GNU Affero General Public License v3.0](LICENSE).

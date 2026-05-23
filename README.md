# TranscrIA

**Service de transcription et valorisation de réunions** — TranscrIA transforme un enregistrement audio/vidéo long en livrables exploitables : transcription SRT horodatée, résumé structuré, identification des locuteurs, lexique métier, rapport qualité et package ZIP final.

---

## Fonctionnalités

- **Transcription ASR** — Cohere Transcribe par défaut, Whisper large-v3 qualité/fallback via faster-whisper, sélection automatique sur audio dégradé
- **Diarisation** avec pyannote community-1, exclusive turns, cache de checkpoint et extraits audio par locuteur
- **Analyse de scène audio** — subprocess CPU isolé (librosa) : classification speech/music/noise via énergie RMS + flatness spectrale + ZCR, estimation du genre H/F par pitch YIN ; résultat disponible dans l'UI et le contexte LLM
- **Séparation de sources** optionnelle (Demucs) : déclenchée automatiquement si musique détectée ou score qualité dégradé
- **Résumé LLM** via opencode CLI et une LLM locale/OpenAI-compatible configurée
- **Correction SRT** par LLM (orthographe, lexique métier, attribution locuteurs)
- **Contrôle qualité** automatisé (score /100, diagnostics ASR/VAD, seuils configurables)
- **Export ZIP** complet (SRT, contexte, participants, lexique, rapport qualité)
- **Interface web** guidée en 9 étapes (wizard Flask + Bootstrap 5)
- **Worker interne** sérialisé pour exécuter les traitements longs hors requête HTTP
- **Authentification**, rôles, groupes utilisateurs, admins de groupe et visibilité partagée des jobs
- **Configuration** YAML avec validation, détection automatique de l'environnement et interface admin
- **Cycle GPU** orchestré (STT → pyannote → LLM d'arbitrage), context manager GPUSession, choix STT qualité piloté par diagnostics

## Stack technique

| Composant | Technologie |
|---|---|
| Backend | Python 3.11+, Flask 3.x, SQLAlchemy, SQLite |
| Frontend | Jinja2, Bootstrap 5, JavaScript vanilla |
| ASR | Cohere Transcribe 03-2026 par défaut ; Whisper large-v3/faster-whisper en qualité ou audio dégradé |
| Diarisation | pyannote community-1 + exclusive turns + checkpoints locuteurs |
| LLM | opencode CLI + backend OpenAI-compatible configuré : script (llama.cpp), Ollama, HTTP, SGLang, vLLM, etc. |
| GPU | VRAMManager, GPUSession (context manager), LLMBackend (script/ollama/http) |
| Monitoring | Dashboard LLM (optionnel, port 5001) |
| Éditeur SRT | SRT Editor EASY (optionnel, port 7861) |

## Prérequis

- Python 3.11+
- GPU(s) NVIDIA avec CUDA 12.x :
  - 1 GPU 16 Go minimum pour le pipeline ASR seul
  - 2+ GPUs 24 Go recommandés pour le cycle complet avec une LLM locale 30B/35B quantifiée (~48 Go VRAM selon modèle/backend)
- ffmpeg / ffprobe (`apt install ffmpeg`)
- opencode CLI (pour le moteur LLM)
- Les modèles IA pré-téléchargés (l'application fonctionne en mode offline)

## Installation rapide

```bash
git clone https://github.com/Martossien/transcria.git
cd transcria
./install.sh
```

`install.sh` prend en charge toute l’installation en une seule commande :

- Détecte la version CUDA et installe le bon wheel PyTorch
- Crée le venv, installe toutes les dépendances
- Génère `config.yaml` via auto-détection (opencode, ffmpeg, chemins modèles)
- Vérifie la présence des modèles IA (Cohere ASR, faster-whisper, pyannote, modèle LLM local configuré) et propose de télécharger les manquants quand c'est possible
- Guide interactivement les valeurs critiques (mot de passe admin, HF_TOKEN, chemin opencode)
- Installe et active le service systemd

Options disponibles :

```bash
./install.sh --help           # Afficher toutes les options
./install.sh --no-service     # Sans installation systemd
./install.sh --no-torch       # PyTorch déjà installé
./install.sh --cuda cu124     # Forcer la version CUDA
./install.sh --hf-token TOKEN # Token HuggingFace pour pyannote
./install.sh --non-interactive # Mode CI/script (pas de prompts)
```

> Guide complet (modèles, configuration, dépannage, service systemd) : **[docs/INSTALL.md](docs/INSTALL.md)**

### Configuration

`install.sh` remplit automatiquement `config.yaml` et guide les valeurs critiques.
Vérifier ensuite en particulier :
- le mot de passe admin initial (`auth.first_admin_password`)
- les chemins des modèles IA si installés hors des répertoires par défaut
- les scripts et endpoints LLM si votre environnement diffère du template

Les noms `qwen_*` encore présents dans certains scripts, tests ou clés de compatibilité sont historiques. Les nouvelles configurations doivent utiliser les noms génériques (`arbitrage_llm_port`, `launch_arbitrage_llm`, `stop_arbitrage_llm`, `llm_cleanup_ports`). Qwen reste seulement le modèle d'exemple du déploiement local fourni.

L’interface d’administration (`/admin/config`) valide la configuration et détecte automatiquement l’environnement (GPUs, binaires, RAM).

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
Si le mot de passe reste `admin-change-me`, un warning explicite est écrit dans les logs au premier démarrage.


### Choix ASR qualité

Le backend normal reste `models.stt_backend` (`cohere` en production). Le mode qualité force `whisper` via `workflow.quality_transcription`. Si le résumé rapide signale un son dégradé, `PipelineService` écrit `metadata/audio_quality_decision.json` et force aussi Whisper, même en mode rapide, selon les seuils configurés dans `workflow.audio_quality`.

Whisper Large V3 ajoute les timestamps mot-à-mot, les garde-fous anti-hallucination, l'alignement CTC optionnel (`whisper.forced_alignment`) et le réalignement locuteur/ponctuation quand les mots traversent plusieurs tours pyannote.

### Gestion des utilisateurs et groupes

Les rôles applicatifs définissent les droits globaux :

| Rôle | Usage |
|---|---|
| `admin` | Administration complète : utilisateurs, groupes, configuration, système, suppression de jobs |
| `manager` | Création, relance, qualité, téléchargement |
| `operator` | Création de jobs, qualité, téléchargement |
| `viewer` | Lecture/téléchargement uniquement |

Fonctions disponibles :
- l'admin global crée et désactive les comptes, change les rôles et réinitialise les mots de passe ;
- chaque utilisateur connecté peut changer son propre mot de passe via **Mot de passe** dans la barre de navigation ;
- en cas d'oubli, le reset passe par un admin global ; le reset email n'est pas activé tant qu'il n'y a pas de configuration SMTP/tokens ;
- l'admin global crée des groupes et désigne un ou plusieurs admins de groupe ;
- les admins de groupe ajoutent ou retirent des utilisateurs existants de leurs groupes ;
- les membres d'un même groupe voient les jobs des autres membres, avec l'indication "Partagé par ..." sur la page d'accueil.

### Supervision

```text
GET /health   -> état du process + base SQLite
GET /ready    -> service prêt à accepter des jobs
GET /metrics  -> métriques Prometheus légères
```

### Workflow (9 étapes)

1. **Fichier** — Dépôt du fichier audio/vidéo
2. **Analyse** — ffprobe (durée, codec, canaux, fréquence)
3. **Résumé** — Transcription rapide Cohere + VAD adaptatif + résumé LLM + diarisation pyannote + analyse de scène (genre H/F)
4. **Contexte** — Suggestions LLM pré-remplies (titre, type, sujet, objectif)
5. **Participants** — Locuteurs détectés avec extraits audio écoutables + indicateur de genre vocal estimé
6. **Lexique** — Termes métier (import .txt/.csv, catégories, priorités)
7. **Traitement** — Transcription finale Cohere/Whisper selon mode et qualité audio + réalignement locuteur mot-à-mot + correction LLM
8. **Qualité** — Score /100, diagnostics ASR/VAD, points de relecture
9. **Export** — Package ZIP (SRT corrigé, contexte, participants, lexique, rapports)

## Tests

```bash
source venv/bin/activate
python -m pytest tests/ -q                          # suite pytest standard, GPU non requis pour la plupart
venv/bin/python tests/test_e2e_workflow.py --skip-llm        # E2E rapide (1 GPU)
venv/bin/python tests/test_e2e_workflow.py                   # E2E complet (GPUs + LLM requis)
venv/bin/python tests/test_e2e_workflow.py --stt-backend whisper  # Avec Whisper large-v3
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
│   ├── auth/                    # Utilisateurs, groupes, rôles, permissions, routes /login
│   ├── jobs/                    # Modèle Job (20 états), CRUD, filesystem
│   ├── workflow/                # Étapes (9), calcul d'état, runner
│   ├── audio/                   # Analyse (ffprobe), conversion (ffmpeg), VAD adaptatif, analyse de scène, filtrage, normalisation, séparation de sources
│   ├── stt/                     # BaseTranscriber, Cohere, Whisper, anti-hallucination
│   │                            #   Transcriber, DiarizerService, SpeakerDetector, SummaryGenerator
│   │                            #   alignement CTC, réalignement locuteur, TranscriberFactory
│   ├── context/                 # Contexte réunion, participants, lexique
│   ├── quality/                 # Checks qualité, score /100, décision qualité audio
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

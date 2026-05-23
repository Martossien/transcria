# TranscrIA

TranscrIA est un portail web de transcription et de valorisation de réunions longues. Il transforme un fichier audio ou vidéo en livrables exploitables : SRT horodaté et corrigé, participants et locuteurs, lexique métier, résumé structuré, rapport qualité et package ZIP final.

Le projet cible un usage opérationnel : dépôt du fichier, diagnostic audio lisible, choix de traitement adapté, contrôle humain des participants/termes, puis transcription finale avec garde-fous contre les hallucinations ASR et les erreurs LLM.

## Fonctionnalités

- **Workflow web guidé** : 9 étapes de l'upload à l'export, avec reprise possible et états persistants.
- **Transcription multi-backend** : Cohere Transcribe par défaut, Whisper large-v3/faster-whisper pour le mode qualité ou les audios diagnostiqués comme dégradés.
- **Diagnostic audio avant transcription** : ffprobe, préflight acoustique, analyse de scène speech/music/noise, ratios non vocaux, estimation de genre vocal H/F quand disponible.
- **Prétraitements contrôlés** : séparation de sources Demucs optionnelle, filtrage scène, normalisation, auto-loudnorm sur voix très faible, denoise expérimental désactivé par défaut.
- **Diarisation pyannote** : tours exclusifs, checkpoints, extraits audio par locuteur, injection du genre vocal par locuteur sans écraser les choix utilisateur.
- **Fiabilité segmentaire** : score `ok|suspect|degrade` par segment, signaux `no_speech_prob`, confiance mot-à-mot, micro-segments et artefacts de sous-titrage.
- **Anti-hallucination ASR** : réduction de boucles répétitives pour Cohere et Whisper, nettoyage post-STT configurable.
- **LLM d'arbitrage locale/OpenAI-compatible** : résumé structuré, rôles probables des locuteurs, termes douteux à valider, correction SRT avec lexique et contexte.
- **Interface utilisateur sobre** : diagnostic audio visible après analyse, options recommandées et options avancées, sans noyer l'utilisateur dans les détails techniques.
- **Contrôle qualité** : score /100, rapport JSON/Markdown, points de relecture, diagnostics de transcription.
- **Gestion multi-utilisateurs** : authentification, rôles, groupes, admins de groupe, visibilité partagée des jobs.
- **Voix enregistrées avec consentement** : référentiel admin/admin groupe, formulaire PDF vierge, preuve signée hashée, empreinte vocale locale, suppression de l'audio source par défaut et suggestions de matching validées humainement.
- **Orchestration GPU** : VRAMManager, GPUSession, choix du meilleur GPU libre, cycle STT/pyannote/LLM et nettoyage des backends concurrents.
- **Tests et benchmarks** : suite pytest mockée, E2E GPU réel, runner benchmark multi-combinaisons pour comparer Cohere/Whisper et les options audio.

## Stack technique

| Domaine | Technologie |
|---|---|
| Backend | Python 3.11+, Flask 3.x, SQLAlchemy, SQLite |
| Frontend | Jinja2, Bootstrap 5, JavaScript vanilla |
| ASR | Cohere Transcribe 03-2026, faster-whisper large-v3 |
| Diarisation | pyannote.audio community-1, exclusive turns, checkpoints |
| Audio | ffmpeg/ffprobe, librosa en subprocess, Silero VAD, Demucs optionnel |
| LLM | opencode CLI + backend OpenAI-compatible local ou distant |
| GPU | NVIDIA CUDA, VRAMManager, GPUSession |
| Supervision | `/health`, `/ready`, `/metrics`, dashboard LLM optionnel |
| Édition SRT | SRT Editor EASY optionnel |

## Prérequis

- Python 3.11+
- ffmpeg et ffprobe
- GPU(s) NVIDIA avec CUDA 12.x pour le pipeline complet
- Les modèles ASR/diarisation/LLM disponibles localement ou préconfigurés
- `opencode` pour les phases LLM de résumé et correction
- Token Hugging Face si le modèle pyannote choisi le requiert

Ordres de grandeur GPU :

- Cohere ASR : environ 6 Go VRAM
- Whisper large-v3 : environ 10 Go VRAM selon `compute_type`
- pyannote : environ 2 Go VRAM
- LLM locale 30B/35B quantifiée : typiquement 48 à 60 Go VRAM selon backend/modèle

## Installation rapide

```bash
git clone https://github.com/Martossien/transcria.git
cd transcria
./install.sh
```

`install.sh` crée le venv, installe les dépendances, choisit le wheel PyTorch adapté à CUDA, génère `config.yaml`, guide les valeurs critiques et peut installer le service systemd.

Options utiles :

```bash
./install.sh --help
./install.sh --no-service
./install.sh --no-torch
./install.sh --cuda cu126
./install.sh --hf-token TOKEN
./install.sh --non-interactive
```

Guide complet : [docs/INSTALL.md](docs/INSTALL.md).

## Configuration

La configuration applicative est dans `config.yaml` (non versionné). Le template complet est [config.example.yaml](config.example.yaml), et la référence détaillée est [docs/CONFIG_REFERENCE.md](docs/CONFIG_REFERENCE.md).

Points à vérifier après installation :

- `auth.first_admin_password` : changer la valeur initiale avant usage réel.
- `models.*` : chemins ou noms des modèles Cohere, Whisper et pyannote.
- `services.arbitrage_*` : script, port et alias réel du backend LLM.
- `workflow.summary_llm.model_id` et `workflow.arbitration_llm.model_id` si les phases LLM sont activées.
- `security.max_upload_size_mb` et extensions autorisées selon l'environnement.
- `voice_enrollment.enabled` si le référentiel de voix connues doit être activé, avec `voice_enrollment.storage_dir` placé sur un stockage local protégé.

Variables d'environnement principales :

| Variable | Description |
|---|---|
| `TRANSCRIA_CONFIG` | Chemin du fichier de configuration |
| `TRANSCRIA_SECRET` | Clé secrète Flask |
| `TRANSCRIA_HOST` / `TRANSCRIA_PORT` | Adresse d'écoute |
| `TRANSCRIA_DEBUG` | Force le mode debug |
| `HF_TOKEN` | Token Hugging Face |
| `TRANSCRIA_OPENCODE_BIN` | Chemin du binaire opencode |
| `TRANSCRIA_PREFERRED_GPU` | GPU physique préféré par le VRAMManager |

Les anciennes références `qwen_*` restent des aliases de compatibilité ou des exemples historiques. Le contrat actuel est générique : une LLM d'arbitrage OpenAI-compatible configurée par `services.*` et `workflow.*.model_id`.

## Lancement

Développement :

```bash
source venv/bin/activate
python app.py
```

Service systemd :

```bash
sudo systemctl restart transcria.service
sudo systemctl status transcria.service
```

Scripts legacy :

```bash
./start.sh
./status.sh
./stop.sh
```

L'interface est disponible par défaut sur `http://localhost:7870`. Au premier démarrage, le compte admin initial est créé depuis `config.yaml`; un warning est logué si le mot de passe reste une valeur par défaut.

## Workflow utilisateur

1. **Fichier** : upload audio/video.
2. **Analyse** : ffprobe, contrôle format, diagnostic audio visible.
3. **Résumé** : transcription rapide, VAD, diarisation, analyse de scène, résumé LLM si activé.
4. **Contexte** : titre, type, sujet, objectifs et suggestions LLM.
5. **Participants & Locuteurs** : validation des locuteurs, extraits audio, genre vocal estimé si disponible.
6. **Lexique** : termes métier, variantes, priorités, contextes proposés avec écoute audio, import TXT/CSV.
7. **Traitement** : prétraitements audio, transcription finale Cohere/Whisper, correction LLM.
8. **Qualité** : rapport, score, diagnostics, segments suspects.
9. **Export** : package ZIP final.

Le choix Cohere/Whisper n'est pas réduit à "fast vs quality". Le pipeline conserve le backend configuré, mais peut forcer Whisper sur mode qualité ou audio dégradé selon `metadata/audio_quality_decision.json`. Le backend réel est tracé dans `metadata/transcription_metadata.json`.

## Voix enregistrées

Le menu **Voix enregistrées** est réservé aux admins globaux et aux admins de groupe. Il permet de gérer des personnes connues avec une preuve de consentement avant toute vectorisation.

Flux prévu :

1. Télécharger le formulaire vierge depuis `/admin/voices/consent-form.pdf`.
2. Faire signer la personne concernée.
3. Créer la voix dans le groupe concerné.
4. Uploader la preuve signée, conservée dans `voices/subjects/<id>/consents/` avec hash SHA-256.
5. Uploader un audio de référence et générer l'empreinte vocale locale.
6. Dans l'étape **Participants & Locuteurs**, lancer “Rechercher les voix connues” pour obtenir une suggestion par locuteur.
7. Valider manuellement la suggestion avant d'enregistrer le mapping.

Les empreintes ne sont jamais incluses dans les exports de jobs. Les résultats de matching écrits dans `speakers/voice_matches.json` contiennent uniquement des noms candidats, scores et marges, sans vecteur vocal.

## Pipeline audio et STT

Avant la transcription finale, `PipelineService` peut exécuter :

1. `audio_preflight` : RMS, peak, clipping, SNR estimé, bande passante, flags et risque.
2. `audio_scene` : speech/music/noise/noEnergy, segments problématiques, distribution H/F.
3. réévaluation qualité audio : décision backend et signaux de dégradation.
4. séparation de sources Demucs : optionnelle et soumise à décision.
5. filtrage scène : mise en silence de zones non vocales sans décaler les timestamps.
6. denoise : expérimental, désactivé par défaut, activé seulement sur demande ou flags configurés.
7. normalisation : optionnelle, avec auto-loudnorm pour voix très faible.
8. transcription : Cohere ou Whisper, chunking par tours pyannote si possible.
9. post-traitement : nettoyage artefacts, fusion micro-segments, fiabilité par segment.

Artefacts importants par job :

| Fichier | Rôle |
|---|---|
| `metadata/audio_analysis.json` | Analyse ffprobe |
| `metadata/audio_preflight.json` | Diagnostic acoustique déterministe |
| `metadata/audio_scene.json` | Scène audio et genres vocaux |
| `metadata/audio_quality_decision.json` | Décision qualité/backend |
| `metadata/audio_denoise.json` | Trace denoise si appliqué |
| `metadata/audio_normalization.json` | Trace normalisation ou auto-loudnorm |
| `metadata/audio_excerpts/*.wav` | Cache des extraits écoutés pour valider le lexique |
| `metadata/transcription_metadata.json` | Backend réel, chunking, VAD final, stats |
| `quality/quality_report.json` | Checks qualité et score |

## LLM, contexte et correction

Les phases LLM passent par `opencode`. Le résumé peut enrichir `meeting_context.json` avec :

- titre/type/sujet/objectifs suggérés ;
- rôles probables par `SPEAKER_XX` ;
- termes douteux ou suspects à valider ;
- résumé structuré utilisé ensuite comme contexte de correction.

La correction SRT reçoit le contexte de réunion, les participants validés, le lexique utilisateur, les indices de qualité et les segments litigieux. Ces données sont des aides, pas des autorités absolues : les prompts demandent de respecter les noms mappés et le lexique validé, tout en évitant d'inventer des corrections.

Le parsing LLM est volontairement tolérant : il accepte les sorties avec Markdown et plusieurs formats de listes, puis conserve les avertissements de parsing pour diagnostic au lieu de faire échouer le workflow dans les cas récupérables.

## Gestion des utilisateurs

| Rôle | Capacités principales |
|---|---|
| `admin` | Administration complète : utilisateurs, groupes, configuration, système, suppression |
| `manager` | Création, relance, contrôle qualité, téléchargement |
| `operator` | Création de jobs, qualité, téléchargement |
| `viewer` | Consultation et téléchargement |

Les groupes permettent la visibilité croisée des jobs entre membres. Les admins de groupe peuvent gérer les membres existants de leurs groupes sans disposer des droits admin globaux.

## Tests

Suite standard, sans GPU obligatoire pour la plupart des tests :

```bash
source venv/bin/activate
python -m pytest tests/ -q
python -m pytest tests/test_voice_e2e.py -q
```

E2E réel, à lancer avec le Python du venv :

```bash
venv/bin/python tests/test_e2e_workflow.py --skip-llm
venv/bin/python tests/test_e2e_workflow.py --audio tests/test2.mp3 --keep
venv/bin/python tests/test_e2e_workflow.py --stt-backend whisper --mode quality
```

Benchmarks audio multi-combinaisons :

```bash
venv/bin/python scripts/bench_audio.py --help
venv/bin/python scripts/bench_analyze.py --help
venv/bin/python scripts/bench_eval.py --help
```

Documentation E2E : [tests/E2E_README.md](tests/E2E_README.md).

## Structure du projet

```text
transcria/
  app.py                         # create_app() + main()
  config.example.yaml            # template complet de configuration
  install.sh                     # installation guidée
  transcria/
    auth/                        # utilisateurs, groupes, permissions
    audio/                       # ffprobe, preflight, scene, denoise, normalisation, Demucs, VAD
    config/                      # loader, schema, détection système
    context/                     # réunion, participants, lexique, job_context
    exports/                     # package ZIP
    gpu/                         # VRAMManager, GPUSession, opencode, backends LLM
    jobs/                        # Job, JobStore, filesystem
    quality/                     # checks qualité, rapport, points de relecture
    services/                    # JobService, PipelineService, worker, ConfigService
    stt/                         # Cohere, Whisper, diarisation, alignement, fiabilité
    voice/                       # voix enregistrées, consentements, empreintes, matching
    web/                         # routes Flask, templates, JS
    workflow/                    # étapes, transitions, runner
  configs/prompts/               # prompts summary/correction
  scripts/                       # bootstrap, LLM, bench audio
  tests/                         # pytest + E2E GPU réel
  docs/
    forms/                       # sources éditables des formulaires PDF
    ...                          # documentation projet
```

## Documentation

| Document | Contenu |
|---|---|
| [docs/INSTALL.md](docs/INSTALL.md) | Installation, modèles, systemd, dépannage |
| [docs/TECHNICAL.md](docs/TECHNICAL.md) | Architecture, pipeline, API, GPU |
| [docs/DATA_MODEL.md](docs/DATA_MODEL.md) | États, transitions, fichiers par job |
| [docs/CONFIG_REFERENCE.md](docs/CONFIG_REFERENCE.md) | Référence complète `config.yaml` |
| [tests/E2E_README.md](tests/E2E_README.md) | Utilisation du test E2E et options benchmark |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Guide de contribution |
| [SECURITY.md](SECURITY.md) | Politique de sécurité |
| [CHANGELOG.md](CHANGELOG.md) | Historique des changements |

## Licence

TranscrIA est distribué sous licence [GNU Affero General Public License v3.0](LICENSE).

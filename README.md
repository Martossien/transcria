# TranscrIA

[![CI](https://github.com/Martossien/transcria/actions/workflows/tests.yml/badge.svg)](https://github.com/Martossien/transcria/actions/workflows/tests.yml)

TranscrIA est un portail web de transcription et de valorisation de réunions longues. Il transforme un fichier audio ou vidéo en livrables exploitables : SRT horodaté et corrigé, participants et locuteurs, lexique métier, résumé structuré, rapport qualité et package ZIP final.

Le projet cible un usage opérationnel : dépôt du fichier, diagnostic audio lisible, choix de traitement adapté, contrôle humain des participants/termes, puis transcription finale avec garde-fous contre les hallucinations ASR et les erreurs LLM.

## Fonctionnalités

- **Workflow web guidé** : 9 étapes de l'upload à l'export, avec reprise possible et états persistants.
- **Transcription multi-backend** : Cohere Transcribe par défaut ; Whisper large-v3/faster-whisper et IBM Granite Speech 4.1 2B restent disponibles pour les tests, fallbacks et usages ciblés. Parakeet TDT 0.6B v3 (NVIDIA NeMo) en backend expérimental.
- **Diagnostic audio avant transcription** : ffprobe, préflight acoustique, analyse de scène speech/music/noise, ratios non vocaux, estimation de genre vocal H/F quand disponible.
- **Prétraitements contrôlés** : séparation de sources Demucs optionnelle, filtrage scène, normalisation, auto-loudnorm sur voix très faible, denoise expérimental désactivé par défaut.
- **Diarisation multi-backend** : pyannote.audio (défaut, tours exclusifs, checkpoints, extraits audio) ou NVIDIA Sortformer 4spk via NeMo (jusqu'à 4 locuteurs, segments exclusifs natifs). Backend sélectionné par `models.diarization_backend`. Injection du genre vocal par locuteur sans écraser les choix utilisateur.
- **Fiabilité segmentaire** : score `ok|suspect|degrade` par segment, signaux `no_speech_prob`, confiance mot-à-mot, micro-segments et artefacts de sous-titrage.
- **Anti-hallucination ASR** : réduction de boucles répétitives pour Cohere, Whisper et Granite, nettoyage post-STT configurable.
- **LLM d'arbitrage locale/OpenAI-compatible** : résumé structuré, rôles probables des locuteurs, termes douteux à valider, correction SRT avec lexique et contexte.
- **Lexiques centralisés par groupe** : référentiel admin/admin groupe, pré-remplissage du lexique de session, fusion avec les suggestions LLM et filtrage avant correction.
- **Interface utilisateur sobre** : diagnostic audio visible après analyse, options recommandées et options avancées, sans noyer l'utilisateur dans les détails techniques.
- **Contrôle qualité** : score /100, rapport JSON/Markdown, points de relecture, diagnostics de transcription.
- **Gestion multi-utilisateurs** : authentification, rôles, groupes, admins de groupe, visibilité partagée des jobs.
- **Notifications email** : email de fin de traitement envoyé au propriétaire du job (succès ou échec), configurable via SMTP/STARTTLS/SMTPS, fire-and-forget en tâche de fond.
- **File GPU persistante** : mise en file activée par défaut, priorités, pause/reprise/annulation, démarrage différé (`scheduled_at`), profil VRAM par job, priority aging (bonus croissant contre la famine), limites de concurrence dynamiques.
- **Planification des ressources par calendrier** : plages horaires par jour de semaine (timezone configurable, fenêtres à cheval sur minuit), 4 actions — `pause_queue`, `limit_concurrency`, `force_gpu`, `none` — avec résolution par priorité. CRUD auditable via l'interface admin.
- **Piste d'audit RGPD** : enregistrement de chaque action sensible (login/logout, accès/téléchargement/suppression de job, modifications lexique, gestion des voix, édition config, gestion users/groupes, opérations de planification) avec acteur, IP, user-agent et timestamp. Filtrable par acteur, action, période et cible. Exportable.
- **Voix enregistrées avec consentement** : référentiel admin/admin groupe, formulaire PDF vierge, preuve signée hashée, empreinte vocale locale, suppression de l'audio source par défaut et suggestions de matching validées humainement.
- **Orchestration GPU** : VRAMManager, GPUSession, choix du meilleur GPU libre, CUDA_VISIBLE_DEVICES remapping, cycle STT/pyannote/LLM et nettoyage des backends concurrents.
- **Inférence distante (deux topologies)** : TranscrIA tourne soit en tout-en-un (ressources GPU locales), soit en **frontale** dont le STT (serveur compatible OpenAI : vLLM, SGLang…), la diarisation et l'empreinte vocale sont servis par un **nœud de ressources** distant. Moteur de serving non hardcodé, autonomie VRAM du STT (cycle A/B/C : réutilise / lance à la demande / 503 quand saturé), pré-check VRAM + relocalisation GPU optionnelle, transcription par tour concurrente, panneau d'état des ressources et mode dégradé (file/échec explicite). Voir `docs/SERVICE_RESSOURCES_GPU.md`.
- **Rapport Word (.docx) adapté au type de réunion** : document professionnel généré automatiquement en fin de workflow, téléchargeable directement ou inclus dans le package ZIP. Trois niveaux d'adaptation au type de réunion :
  - **Extraction structurée par la LLM** : décisions prises, actions à réaliser, points bloquants, points reportés, votes, résolutions, ordre du jour et prochaine date sont extraits du résumé via un prompt universel et un parseur tolérant (3 niveaux de repli `ok`/`partiel`/`échec`, dégradation gracieuse vers le rapport standard si l'extraction échoue). Affichés dans le document selon le type.
  - **Champs spécifiques au type** : 18 types de réunion (CSE, CSE extraordinaire, CODIR/COMEX, Point projet, Réunion client, Réunion de crise, Entretien individuel, Formation, Séminaire, Négociation…). Chaque type affiche dans l'interface des champs dédiés (président/secrétaire/quorum CSE, nom de projet/sprint, client/contrat…) repris dans le document et injectés dans le contexte LLM de correction.
  - **Thèmes visuels par type** : page de garde, titres de section, tableaux et pied de page adoptent une identité de couleur cohérente selon le type (bleu marine institutionnel CSE, teal projet, rouge crise, violet confidentiel…), avec bannière dédiée, badge confidentiel/crise et calcul automatique du quorum CSE.
- **Tests et benchmarks** : suite pytest mockée (1031+ tests), E2E GPU réel, E2E automatisés sans GPU pour le pipeline DOCX, runner benchmark multi-combinaisons pour comparer Cohere/Whisper/Granite et les options audio.

## Stack technique

| Domaine | Technologie |
|---|---|
| Backend | Python 3.11+, Flask 3.x, SQLAlchemy, SQLite, python-docx |
| Frontend | Jinja2, Bootstrap 5, JavaScript vanilla |
| ASR | Cohere Transcribe 03-2026, faster-whisper large-v3, Granite Speech 4.1 2B expérimental, Parakeet TDT 0.6B v3 expérimental (NeMo) |
| Diarisation | pyannote.audio community-1 (défaut), NVIDIA Sortformer 4spk v2.1 (NeMo) — factory pattern, BaseDiarizer ABC |
| Audio | ffmpeg/ffprobe, librosa en subprocess, Silero VAD, Demucs optionnel |
| LLM | opencode CLI + backend OpenAI-compatible local ou distant |
| GPU | NVIDIA CUDA, VRAMManager, GPUSession |
| Supervision | `/health`, `/ready`, `/metrics`, dashboard LLM optionnel |
| Édition SRT | SRT Editor EASY optionnel |
| Analyse VAD | `docs/VAD_OR_NOT.md` — recommandations par type de fichier |

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
- Granite Speech 4.1 2B : environ 6 Go VRAM, expérimental et désactivé par défaut
- Parakeet TDT 0.6B v3 : environ 8 Go VRAM, expérimental (NeMo)
- pyannote community-1 : environ 2 Go VRAM (backend diarisation par défaut)
- Sortformer 4spk v2.1 : environ 3.5 Go VRAM (backend diarisation alternatif, NeMo)
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
- `workflow.queue.*`, `workflow.execution.max_concurrent_jobs` et `workflow.scheduling.*` pour la file persistante, le parallélisme et les créneaux calendrier.
- `security.max_upload_size_mb` et extensions autorisées selon l'environnement.
- `voice_enrollment.enabled` si le référentiel de voix connues doit être activé, avec `voice_enrollment.storage_dir` placé sur un stockage local protégé.
- `notifications.email.enabled` + `smtp_host` / `smtp_port` / `from_address` / `base_url` pour activer les emails de fin de traitement (succès/échec) vers le propriétaire du job. L'adresse email doit être renseignée dans le profil de chaque utilisateur.
- Les lexiques centralisés sont stockés en base SQLite et ne nécessitent pas de section config dédiée en V1.

Variables d'environnement principales :

| Variable | Description |
|---|---|
| `TRANSCRIA_CONFIG` | Chemin du fichier de configuration |
| `TRANSCRIA_SECRET` | Clé secrète Flask |
| `TRANSCRIA_HOST` / `TRANSCRIA_PORT` | Adresse d'écoute |
| `TRANSCRIA_DEBUG` | Force le mode debug |
| `HF_TOKEN` | Token Hugging Face |
| `TRANSCRIA_OPENCODE_BIN` | Chemin du binaire opencode |
| `TRANSCRIA_PREFERRED_GPU` | GPU préféré par le VRAMManager/GPUAllocator (ordinal CUDA visible si `CUDA_VISIBLE_DEVICES` est défini) |
| `CUDA_VISIBLE_DEVICES` | Masque CUDA optionnel ; les ids physiques sont remappés vers `cuda:0..N` avant chargement modèle |

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
4. **Contexte** : titre, type de réunion (18 types), sujet, objectifs et suggestions LLM. Le choix du type fait apparaître des champs spécifiques (président/secrétaire/quorum pour un CSE, nom de projet/sprint pour un point projet, etc.) et conditionne le thème visuel et les sections du rapport Word final.
5. **Participants & Locuteurs** : validation des locuteurs, extraits audio, genre vocal estimé si disponible.
6. **Lexique** : termes métier, variantes, priorités, contextes proposés avec écoute audio, import TXT/CSV. Les lexiques centralisés accessibles au job pré-remplissent la session tant qu'un lexique utilisateur n'a pas déjà été sauvegardé.
7. **Traitement** : prétraitements audio, transcription finale Cohere/Whisper/Granite, correction LLM.
8. **Qualité** : rapport, score, diagnostics, segments suspects.
9. **Export** : rapport Word (.docx) téléchargeable directement + package ZIP final (rapport inclus).

Le choix du backend STT n'est pas réduit à "fast vs quality". Le mode qualité active le workflow complet, mais conserve le backend configuré par défaut (`cohere`). Un forçage Whisper ou Granite reste possible par configuration pour des campagnes ciblées. Le backend réel est tracé dans `metadata/transcription_metadata.json`.

## File GPU et planification

`POST /api/jobs/<id>/process` ne lance plus directement le traitement dans la requête HTTP : le job est mis en file dans `job_queue`, puis `QueueScheduler` le dispatch en arrière-plan selon la priorité, l'heure planifiée, la capacité worker et l'état GPU. La file est activée par défaut (`workflow.queue.enabled=true`) avec une concurrence par défaut de 1 pour préserver le comportement historique.

En fin de traitement via file, le worker publie les états terminaux dans un ordre cohérent pour les APIs de polling : `job_queue.status` devient `done`/`failed`/`cancelled`, puis `extra_data.execution.status`, puis `jobs.state`. Cela évite qu'un client voie un job `completed` alors que la file le signale encore `running`.

Les admins globaux et les admins de groupe peuvent gérer la file : les admins globaux voient tous les jobs, les admins de groupe uniquement les jobs des membres de leurs groupes. Les actions sensibles sont auditées (`job_enqueue`, `job_dequeue`, `job_prioritize`, `job_reorder`, `queue_pause`, `queue_resume`). Les admins globaux disposent aussi d'un bouton de nettoyage des jobs de test dont le titre commence par `E2E workflow`; les jobs en cours sont ignorés et l'action est auditée (`job_test_purge`).

Pages et API principales :

| Chemin | Rôle |
|---|---|
| `/admin/queue` | Vue de la file, runtime scheduler, actions pause/reprise/annulation/réordonnancement |
| `/admin/schedule` | Gestion des créneaux de planification |
| `/api/queue/status` | Snapshot runtime de la file |
| `/api/queue/<job_id>/move-up`, `move-down`, `pause`, `resume`, `priority`, `cancel` | Mutations de file auditées |
| `/api/schedule/windows` | CRUD JSON des créneaux |

Les règles calendrier supportées sont :

- `pause_queue` : règle on/off ; aucun nouveau job n'est dispatché, les jobs en cours continuent ;
- `limit_concurrency` : règle paramétrée ; réduit temporairement le nombre de jobs simultanés via `action_params.max_concurrent_jobs` ;
- `force_gpu` : règle on/off ; autorise la libération forcée de GPU via les patterns explicitement configurés, uniquement dans la fenêtre active ;
- `none` : aucune règle.

Le calendrier ne demande pas un nombre de GPU. Sur une machine où la LLM d'arbitrage peut occuper plusieurs GPUs, la décision fiable reste dans `GPUAllocator`, qui vérifie la VRAM réelle au moment du dispatch et des phases pipeline.
La libération forcée ne tue que les processus externes correspondant à `workflow.scheduling.kill_patterns`; les processus hors liste sont laissés intacts même s'ils consomment beaucoup de VRAM.

## Voix enregistrées

Le menu **Voix enregistrées** est réservé aux admins globaux et aux admins de groupe. Il permet de gérer des personnes connues avec une preuve de consentement avant toute vectorisation.

Flux prévu :

1. Télécharger le formulaire vierge depuis `/admin/voices/consent-form.pdf`.
2. Faire signer la personne concernée.
3. Créer la voix dans le groupe concerné.
4. Uploader la preuve signée, conservée dans `voices/subjects/<id>/consents/` avec hash SHA-256.
5. Vérifier ou corriger le genre validé sur la fiche voix, uploader un audio de référence et générer l'empreinte vocale locale.
6. Dans l'étape **Participants & Locuteurs**, lancer “Rechercher les voix connues” pour obtenir une suggestion par locuteur.
7. Valider manuellement la suggestion avant d'enregistrer le mapping.

Le genre issu d'une voix enregistrée est traité comme une donnée validée par l'utilisateur : quand la suggestion est acceptée dans l'étape 5, il remplace l'estimation acoustique. La fiche voix permet aussi de rouvrir la preuve signée pour audit. Les empreintes ne sont jamais incluses dans les exports de jobs. Les résultats de matching écrits dans `speakers/voice_matches.json` contiennent uniquement des noms candidats, scores, marges et genre validé, sans vecteur vocal.

## Lexiques centralisés

Le menu **Lexiques** est réservé aux admins globaux et aux admins de groupe. Il permet de maintenir des termes sensibles réutilisables par groupe : forme validée, variantes fréquentes, catégorie, priorité et commentaire.

Règles principales :

- un admin global peut créer un lexique global ou de groupe ;
- un admin de groupe ne peut créer et modifier que les lexiques de ses groupes ;
- un membre simple ne peut pas administrer le référentiel ;
- un job reçoit les lexiques du propriétaire du job et les lexiques globaux, même si un admin consulte le job ;
- le lexique de session sauvegardé par l'utilisateur reste prioritaire et n'est pas écrasé au rechargement.

Pendant l'étape **Lexique de session**, TranscrIA fusionne les entrées centrales avec les termes douteux proposés par la LLM. Avant la correction SRT, le lexique transmis à la LLM est filtré : les entrées présentes dans le SRT par terme ou variante sont conservées, les priorités `critique` et `importante` restent en préservation, et les entrées normales absentes sont retirées du prompt pour réduire le bruit.

Pour rester lisible quand un groupe possède beaucoup d'entrées, l'étape 6 propose aussi une sélection simple des lexiques utilisés pour le job. Les lexiques sont cochés par défaut, l'utilisateur peut en retirer un hors sujet, puis appliquer la sélection. TranscrIA pré-remplit alors seulement les termes les plus utiles : occurrences détectées, variantes détectées et priorités fortes. Les termes normaux peu probables sont masqués de l'affichage, sans être supprimés du référentiel central.

Chaque terme central affiché dans le workflow indique simplement pourquoi il est proposé : trouvé dans le texte, variante détectée ou priorité forte. Côté administration, les fiches lexique affichent les usages réels, les entrées les plus utilisées et des contrôles qualité simples pour repérer les termes trop courts, doublons proches ou variantes inutiles.

Innovations expérimentales : quand le backend effectif est Whisper, TranscrIA peut injecter les termes `critique` et `importante` du lexique de session dans les hotwords Whisper (`whisper.lexicon_hotwords.enabled`). Pour Cohere, une option séparée (`cohere.lexicon_biasing.enabled`) construit un Trie de termes validés et applique un léger biasing contextuel pendant le décodage. Ces deux options sont désactivées par défaut et loguées, car un biasing trop large peut introduire des faux positifs et doit être benchmarké par domaine.

## Pipeline audio et STT

Avant la transcription finale, `PipelineService` peut exécuter :

1. `audio_preflight` : RMS, peak, clipping, SNR estimé, bande passante, flags et risque.
2. `audio_scene` : speech/music/noise/noEnergy, segments problématiques, distribution H/F.
3. réévaluation qualité audio : décision backend et signaux de dégradation.
4. séparation de sources Demucs : optionnelle et soumise à décision.
5. filtrage scène : mise en silence de zones non vocales sans décaler les timestamps.
6. denoise : expérimental, désactivé par défaut, activé seulement sur demande ou flags configurés.
7. normalisation : optionnelle, avec auto-loudnorm pour voix très faible.
8. transcription : Cohere, Whisper ou Granite, chunking par tours pyannote si possible.
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

La correction SRT reçoit le contexte de réunion, les participants validés, le lexique utilisateur filtré, les indices de qualité et les segments litigieux. Ces données sont des aides, pas des autorités absolues : les prompts demandent de respecter les noms mappés et le lexique validé, tout en évitant d'inventer des corrections.

Le parsing LLM est volontairement tolérant : il accepte les sorties avec Markdown et plusieurs formats de listes, puis conserve les avertissements de parsing pour diagnostic au lieu de faire échouer le workflow dans les cas récupérables.

## Gestion des utilisateurs

| Rôle | Capacités principales |
|---|---|
| `admin` | Administration complète : utilisateurs, groupes, configuration, système, suppression |
| `manager` | Création, relance, contrôle qualité, téléchargement |
| `operator` | Création de jobs, qualité, téléchargement |
| `viewer` | Consultation et téléchargement |

Les groupes permettent la visibilité croisée des jobs entre membres. Les admins de groupe peuvent gérer les membres existants de leurs groupes sans disposer des droits admin globaux.

### Audit de sécurité

Toutes les actions sensibles (connexion, modification de job, suppression, accès aux consentements vocaux, édition de configuration, gestion des utilisateurs et des lexiques centralisés) sont journalisées dans une table `audit_logs` horodatée avec identité de l'acteur, adresse IP et détail de l'opération. Les entrées sont conservées 3 ans par défaut (`security.audit_retention_days`) avec surcharge possible par famille (`security.audit_retention_by_family`), consultables et exportables en CSV depuis `/admin/audit` par le responsable sécurité/DPO. L'export du journal d'audit est lui-même journalisé. Aucune route ne permet de supprimer les entrées d'audit.

Les lexiques centralisés ont une traçabilité dédiée RGPD/PSSI : ajout/modification/suppression d'entrée, import, export CSV, changement de périmètre et rattachement à un job. L'audit journalise les volumes, catégories, priorités, groupe/job concerné et signaux de noms propres possibles, mais jamais les termes ou variantes en clair. L'export CSV est une action volontaire côté serveur (`POST`) et peut être réservé aux admins globaux avec `security.lexicon_export_admin_only`.

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
venv/bin/python tests/test_e2e_workflow.py --audio tests/test2.mp3 --skip-summary --skip-llm --skip-diarization --schedule-case pause_then_release
venv/bin/python tests/test_e2e_workflow.py --audio tests/test2.mp3 --skip-summary --skip-llm --skip-diarization --process-via-api
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
    context/                     # réunion, participants, lexique session/centralisé, job_context
    exports/                     # rapport DOCX + package ZIP
    gpu/                         # VRAMManager, GPUSession, opencode, backends LLM
    jobs/                        # Job, JobStore, filesystem
    quality/                     # checks qualité, rapport, points de relecture
    queue/                       # file persistante, scheduler, calendrier, allocation GPU
    services/                    # JobService, PipelineService, worker, ConfigService
    stt/                         # Cohere, Whisper, Granite, Parakeet, BaseDiarizer, DiarizerService (pyannote), SortformerDiarizer (NeMo), diarizer_factory, alignement, fiabilité
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
| [docs/INSTALL.md](docs/INSTALL.md) | Installation, modèles, systemd, dépannage, **déploiement distribué** (frontale + nœud de ressources) |
| [docs/TECHNICAL.md](docs/TECHNICAL.md) | Architecture, pipeline, API, GPU |
| [docs/DATA_MODEL.md](docs/DATA_MODEL.md) | États, transitions, fichiers par job |
| [docs/CONFIG_REFERENCE.md](docs/CONFIG_REFERENCE.md) | Référence complète `config.yaml` |
| [docs/VAD_OR_NOT.md](docs/VAD_OR_NOT.md) | Analyse VAD, tests, recommandations |
| [docs/PARAKEET_STT_INTEGRATION.md](docs/PARAKEET_STT_INTEGRATION.md) | Intégration Parakeet TDT 0.6B v3 |
| [docs/FEATURE_DOCX_REPORT.md](docs/FEATURE_DOCX_REPORT.md) | Spec technique du rapport Word : sources de données, structure, template, roadmap |
| [docs/STT_ADAPTATIF_ET_HYBRIDE.md](docs/STT_ADAPTATIF_ET_HYBRIDE.md) | Conception : caractérisation audio enrichie + mode hybride STT au segment |
| [docs/MIGRATION_API_SERVEUR_GPU.md](docs/MIGRATION_API_SERVEUR_GPU.md) | Conception : bascule vers API / serveur GPU distant (vLLM, vLLM-omni, service maison) |
| [docs/SERVICE_RESSOURCES_GPU.md](docs/SERVICE_RESSOURCES_GPU.md) | Inférence distante v1 : topologies frontale/ressources, autonomie VRAM du STT (A/B/C), /capabilities, mode dégradé |
| [tests/E2E_README.md](tests/E2E_README.md) | Utilisation du test E2E et options benchmark |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Guide de contribution |
| [SECURITY.md](SECURITY.md) | Politique de sécurité |
| [CHANGELOG.md](CHANGELOG.md) | Historique des changements |

## Licence

TranscrIA est distribué sous licence [GNU Affero General Public License v3.0](LICENSE).

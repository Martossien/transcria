# Déploiement Docker (P5)

> Référence du déploiement conteneurisé de TranscrIA. Suit les invariants de
> `docs/archive/PLAN_EVOLUTION_INSTALLATION.md § P5`. Deux familles d'images :
>
> - **Rôles CPU distribués** (`web`, `scheduler`, `migrate`) : une seule image légère construite
>   par le `Dockerfile` racine (torch CPU) et orchestrée par `docker-compose.yml` (profil `split`).
> - **All-in-one GPU** (`all`) : image **dédiée** `Dockerfile.allinone-gpu` (base CUDA 12.6,
>   **llama.cpp compilé** = LLM d'arbitrage embarquée, **NeMo/Sortformer** pour la diarisation
>   non gated), profil `gpu`. Elle livre le **workflow complet GPU en une commande, sans token**
>   — voir « All-in-one GPU » ci-dessous.
>
> Les deux embarquent opencode (agent des phases LLM). Le nœud de ressources GPU du banc split
> utilise `Dockerfile.resource-node` ; le banc split bâtit `Dockerfile.worker`/`Dockerfile.resource-node`
> en exécutant `install.sh`. **Aucune image n'embarque de poids de modèle** (téléchargés au
> runtime) → l'image all-in-one GPU est **publiable** (cf. § Publication).

## Démarrage rapide (une commande)

De `git clone` à un conteneur qui tourne, sans étape manuelle — `scripts/docker_quickstart.sh`
orchestre tout (prérequis GPU, génération `.env`/`config.yaml`, build avec le bon index
CUDA, `compose up`, vérification `/health`) :

```bash
# Tout-en-un GPU (recommandé pour tester le projet) :
scripts/docker_quickstart.sh                  # → http://localhost:7870

# Image à modèles EMBARQUÉS (zéro-download, hors-ligne, sans le piège du cache hôte) :
scripts/docker_quickstart.sh --bundled        # pull ghcr.io/…:bundled si publiée, sinon build local

# Avec le STT de référence (Cohere, gated) — fournir un token HF :
HF_TOKEN=hf_xxx scripts/docker_quickstart.sh

# Sans GPU (web + scheduler, pas d'inférence locale) :
scripts/docker_quickstart.sh --cpu

# Arrêt :
scripts/docker_quickstart.sh --down
```

> **Deux images GPU.** `:latest` (**slim**) télécharge les modèles au 1ᵉ run dans le cache HF
> hôte. `:bundled` (`--bundled`) **embarque** whisper + Sortformer + Voxtral + MOSS-TD (une passe
> ASR+locuteurs, opt-in) + la LLM 9B → aucun téléchargement, cache HF en **volume nommé** seedé
> depuis l'image (élimine le `[Errno 17] File exists` ci-dessous). Dans les deux cas,
> pyannote/Cohere restent en opt-in `HF_TOKEN` (et Kroko-ASR via la page « Modèles »).
>
> **Runtimes STT servis dans les images GPU (0.3.6).** Les images `:latest`/`:bundled` et
> `resource-node` embarquent les binaires ÉPINGLÉS d'audio.cpp (`qwen3asr`) et parakeet.cpp
> (`nemotron`) sous `/opt/runtimes` (`TRANSCRIA_RUNTIMES_DIR`). Les modèles restent par
> **volume** : `hf download Qwen/Qwen3-ASR-1.7B-hf` (snapshot pur) et le GGUF Nemotron via
> la page « Modèles » — pointer `STT_MODEL` du lanceur sur le chemin monté. Configuration :
> `docs/EXTERNAL_STT_RUNTIMES.md`.
>
> **Monter en gamme de LLM depuis l'image `:bundled`.** L'image embarque le palier 12 Go
> (Qwen3.5-9B), mais `MODELS_DIR` (volume `models`) et `/hf` sont des **volumes inscriptibles** :
> passer `TRANSCRIA_LLM_TIER=16|24|32|48|64` (ou télécharger depuis **Administration → Modèles**)
> **télécharge la LLM plus grosse au runtime** dans le volume persistant — le modèle baké
> n'empêche pas la mise à niveau.

> Le quickstart **vérifie le GPU** avant tout (compute capability ≥ 7.5 **et** VRAM ≥ ~12 Go,
> cf. `transcria.deploy.gpu_preflight`) et échoue tôt avec un message clair plutôt que de laisser
> un crash CUDA survenir au 1ᵉ job.

> **Connexion par défaut** : ouvrir `http://localhost:7870` et se connecter avec **`admin`** /
> **`CHANGE-ME`** (identifiants initiaux du `config.yaml` généré, clé `auth.first_admin_password`,
> appliqués **au tout premier démarrage** = bootstrap de la base). **Changer ce mot de passe avant
> tout usage réel** ; le modifier dans `config.yaml` **après** le bootstrap ne change PAS le mot de
> passe d'un compte déjà créé (le faire alors via l'UI / la gestion des utilisateurs).

Le script est **idempotent** : il ne réécrit pas un `config.yaml`/`.env` existant, génère
des secrets aléatoires, choisit `whisper` (non gated, sans token) si `HF_TOKEN` est absent.
Les sections ci-dessous détaillent chaque étape pour un contrôle manuel.

## Prérequis (ce qu'un utilisateur doit faire)

**1. Accès GPU dans Docker** — n'est PAS géré par `requirements.txt` (dépendances Python)
ni par `install.sh` (installation native) : c'est une configuration de l'hôte Docker,
isolée dans un script dédié, idempotent :

```bash
scripts/setup_docker_gpu.sh          # installe nvidia-container-toolkit + génère la spec CDI + vérifie
scripts/setup_docker_gpu.sh --check  # vérifie seulement (GPU visible en conteneur ?)
```

> Prérequis du script : driver NVIDIA (`nvidia-smi`) + Docker déjà installés (il n'y touche pas).
> Il rend le GPU visible via **CDI** (`--device nvidia.com/gpu=…`).

**2. Modèles STT/diarisation** — deux chemins selon le besoin. **Sans token, TOUT marche**
(transcription + locuteurs + résumé/correction) avec des modèles **non gated** ; le token HF
ne sert qu'à la **qualité de référence** :

| Besoin | STT | Diarisation | Token HF |
|---|---|---|---|
| **Test rapide, zéro friction** | `whisper` (openai/whisper-large-v3, non gated) | `sortformer` (NVIDIA, non gated, **≤4 locuteurs**, expérimental) | ❌ aucun |
| **Qualité de référence (prod)** | `cohere` (CohereLabs, **gated**) | `pyannote` (**gated**, locuteurs illimités) | ✅ requis |

Le quickstart choisit automatiquement la 1re ligne sans `HF_TOKEN`, la 2e avec. Pour la qualité
de référence : (a) accepter les conditions des **DEUX** modèles sur
`huggingface.co/CohereLabs/cohere-transcribe-03-2026` **et**
`huggingface.co/pyannote/speaker-diarization-community-1`, (b) créer un token HF, (c) le
fournir au conteneur (`HF_TOKEN`, ou dans `.env`). Le cache HF de l'hôte est monté dans le conteneur (volume
`/hf`) pour éviter de re-télécharger.

> ⚠️ **Cache hôte pré-rempli par un AUTRE utilisateur.** Le conteneur tourne en **root** ; si
> `HF_CACHE_DIR` pointe sur un cache déjà peuplé par un utilisateur non-root (symlinks/permissions),
> le chargement de **faster-whisper** peut échouer (`[Errno 17] File exists`) → transcription vide.
> Pour un utilisateur **neuf** (cache vide), whisper et Sortformer se téléchargent proprement **sans
> token** (validé E2E). En cas de souci, pointer `HF_CACHE_DIR` sur un **cache dédié au conteneur**
> (répertoire vide) plutôt que de réutiliser un cache hôte hétérogène.
> **L'image `:bundled` (`--bundled`) supprime ce piège** : elle n'utilise pas le cache hôte (cache
> HF en volume nommé `hfcache`, seedé depuis les modèles bakés).

> ⚠️ `transcria.stt.cohere_transcriber` force `HF_HUB_OFFLINE=1` par défaut. En conteneur
> avec un cache fraîchement monté, laisser **`HF_HUB_OFFLINE=0`** (le compose le fait) pour
> que la 1re résolution du modèle gated aboutisse ; ensuite le cache sert les poids.

## Principes

- **`install.sh` n'est jamais l'entrypoint applicatif.** L'image est construite une
  fois ; au runtime, l'entrypoint `python -m transcria.deploy.entrypoint <role>`
  valide les invariants, attend la base, puis **remplace le process** par le serveur
  du rôle. La logique d'installation reste dans `transcria.installer` (réutilisée hors
  conteneur par `install.sh`).
- **Mêmes profils que l'install** : `web`, `scheduler`, `resource-node`, `migrate`.
- **PostgreSQL obligatoire.** SQLite n'est pas un mode de déploiement Docker supporté ;
  l'entrypoint refuse de démarrer un rôle à base applicative sans DSN PostgreSQL.
- **`migrate` est un job one-shot** (`alembic upgrade head`) : les serveurs n'auto-migrent
  pas, ils attendent que la migration dédiée ait réussi.
- **Aucun secret baké dans l'image** : `config.yaml` et `.env` sont fournis par volumes ;
  le DSN par `TRANSCRIA_DATABASE_URL`.

## Schéma cible

```
                 ┌─────────────┐
                 │   db (PG)   │  volume pgdata
                 └──────┬──────┘
            healthy     │
        ┌───────────────┼────────────────┐
        ▼               ▼                 ▼
  ┌───────────┐   ┌───────────┐    ┌─────────────┐
  │  migrate  │   │    web    │    │  scheduler  │
  │ (one-shot)│   │ gunicorn  │    │ app.py      │
  │ alembic   │   │ :7870     │    │ --role …    │
  └───────────┘   └───────────┘    └──────┬──────┘
   completed ─────▶ (gate web/scheduler)   │ volumes : jobs, models
                                            ▼
                              (STT / diarisation : nœuds resource-node
                               externes via inference.mode=remote)
```

Conteneurs **externes** à ce compose :

- **resource-node** (GPU) — STT/diarisation/voix locales. Image à base CUDA (cf.
  ci-dessous), déployée sur l'hôte GPU ; déclarée côté scheduler via
  `inference.mode=remote` + URLs des nœuds.
- **LLM d'arbitrage** — **embarquée dans l'all-in-one GPU** (`Dockerfile.allinone-gpu` :
  llama.cpp compilé + petit GGUF tiré au runtime), lancée à la demande par l'autonomie VRAM.
  Pour les **rôles CPU/split** (`web`/`scheduler`), elle reste **externe** : service
  OpenAI-compatible ou conteneur dédié, hôte/port résolus de façon unique
  (`services.arbitrage_llm_host`/`arbitrage_llm_port`, surchargeables par
  `TRANSCRIA_ARBITRAGE_LLM_HOST` — ex. `host.docker.internal` + `extra_hosts: host-gateway`).

## Matrice des variables

| Variable | Rôles | Obligatoire | Description |
|---|---|---|---|
| `TRANSCRIA_ROLE` | tous | oui (ou argument) | `web` \| `scheduler` \| `resource-node` \| `migrate` \| `all` (all-in-one) |
| `TRANSCRIA_DATABASE_URL` | web, scheduler, migrate | **oui** | DSN PostgreSQL (`postgresql+psycopg://…`). SQLite refusé. |
| `TRANSCRIA_CONFIG` | tous | non (défaut `/app/config.yaml`) | Chemin du `config.yaml` monté |
| `TRANSCRIA_BIND` | web | non (défaut `0.0.0.0:7870`) | Adresse d'écoute gunicorn |
| `TRANSCRIA_WORKERS` | web | non (défaut `4`) | Workers gunicorn |
| `INFERENCE_BIND` / `INFERENCE_PORT` | resource-node | non (défaut `0.0.0.0:8002`) | Écoute du service d'inférence |
| `INFERENCE_THREADS` | resource-node | non (défaut `4`) | Threads gunicorn du nœud |
| `POSTGRES_PASSWORD` | db (+ DSN) | **oui** | Mot de passe du rôle `transcria` (compose) |
| `TRANSCRIA_SECRET` | web, scheduler, all | oui (via `.env`) | Clé Flask (dans `.env` monté) |
| `HF_TOKEN` | all, resource-node (GPU) | si STT/diar gated (Cohere/pyannote) | Token Hugging Face (modèles gated) |
| `HF_CACHE_DIR` | all (compose) | non (défaut `~/.cache/huggingface`) | Cache HF de l'hôte monté dans `/hf` |
| `HF_HUB_OFFLINE` | all, resource-node | non (compose met `0`) | `0` requis pour 1re résolution d'un modèle gated en conteneur |
| `TRANSCRIA_ARBITRAGE_LLM_HOST` | scheduler, all | non (défaut `services.arbitrage_llm_host` ou `127.0.0.1`) | Hôte de la LLM d'arbitrage. Override commun à la sonde `vram_manager` ET au provider opencode (résolution unique) — utile quand la LLM tourne sur l'hôte/un nœud (ex. `host.docker.internal`) |
| `TRANSCRIA_LLM_TIER` | all | non (défaut `12`) | Palier VRAM de la LLM embarquée (12/16/24/32/48/64). Pilote le GGUF téléchargé ET le script de lancement du palier (`scripts/arbitrage_profiles/<tier>gb_*.sh`) |
| `MODELS_DIR` | all | non (défaut `/app/models`) | Répertoire (volume `models`, persistant) où le GGUF d'arbitrage est téléchargé au runtime |
| `TRANSCRIA_ARBITRAGE_SCRIPT` | all | non (déduit du palier) | Override explicite du script de lancement de la LLM (sinon résolu depuis `TRANSCRIA_LLM_TIER`) |
| `TRANSCRIA_DEFAULT_LOCALE` | all, web | non (défaut `fr`) | Langue par défaut de l'interface (`fr`/`en`) — override de `i18n.default_locale` sans éditer le YAML. Le sélecteur navbar et la préférence par utilisateur restent disponibles ; la langue des livrables se règle par job |
| `TRANSCRIA_ALLINONE_IMAGE` | all (compose) | non (défaut `transcria-allinone:latest`) | Réf. de l'image GPU. Pointer un tag de registre (ex. `ghcr.io/<owner>/transcria-allinone:vX`) → le quickstart fait un `pull` au lieu d'un build |

Build-time (`docker build --build-arg`) :

| Arg | Défaut | Description |
|---|---|---|
| `PYTHON_VERSION` | `3.11` | Version de l'image de base Python |
| `TORCH_INDEX_URL` | `…/whl/cpu` | Index des wheels PyTorch (CPU). Image GPU : index CUDA. |

## Procédure de démarrage

1. **Préparer la configuration** (non versionnée, montée au runtime) :
   ```bash
   ./install.sh --profile web --non-interactive --skip-deps --no-service \
       --postgres --pg-defer --pg-host db --pg-user transcria --pg-db transcria --pg-password "$POSTGRES_PASSWORD"
   # ⇒ produit config.yaml + .env localement (à monter). `--pg-defer` écrit le DSN SANS se
   #    connecter : `db` n'est pas résoluble depuis l'hôte et la base n'est pas encore démarrée ;
   #    le schéma est appliqué au runtime par le job `migrate`.
   ```
   ou générer `config.yaml` via `scripts/bootstrap_config.py --profile web` puis remplir `.env`.
2. **Exporter le secret de base** : `export POSTGRES_PASSWORD=…`
3. **Démarrer** (profil `split` = web + scheduler ; `db`/`migrate` sont hors profil) :
   ```bash
   docker compose --profile split up -d --build
   ```
   `db` → healthy → `migrate` (one-shot) → `web` + `scheduler`.

   > Les profils `split` (web+scheduler) et `gpu` (all-in-one) sont **alternatifs — à ne pas
   > activer ensemble** : les deux publient `:7870` (Compose autorise techniquement les deux,
   > mais ce serait un conflit de port). `db`/`migrate` démarrent dans les deux cas.
4. **Vérifier** :
   ```bash
   docker compose ps
   docker compose logs -f migrate     # doit afficher "alembic upgrade head" puis sortir 0
   curl -fsS http://localhost:7870/health
   ```

### Dépannage — `migrate` échoue alors que `db` est *healthy*

`POSTGRES_PASSWORD` n'est appliqué qu'à l'**initialisation du volume** PostgreSQL. Si un volume
de données **préexistant** a été créé avec un autre mot de passe, la base conserve l'ancien et
l'authentification TCP échoue — même si le service répond. `migrate` affiche désormais la **vraie
cause** au lieu d'un « injoignable » trompeur :

```
[ERROR] base PostgreSQL inaccessible après 30 tentatives — AUTHENTIFICATION refusée (mot de passe)…
```

Trois remédiations, selon que vous voulez garder les données :
- **réutiliser** le mot de passe d'origine dans `POSTGRES_PASSWORD` ; ou
- **réinitialiser** le volume (⚠ efface les données) : `docker compose down -v` puis relancer ; ou
- **changer** le mot de passe du rôle sans perdre les données :
  `docker compose exec db psql -U transcria -d transcria -c "ALTER USER transcria WITH PASSWORD '<nouveau>';"`.

## GPU (validé)

Le GPU dans Docker passe par **CDI** (Container Device Interface). Setup hôte, une fois :

```bash
# 1. Toolkit conteneur NVIDIA (ne touche pas le driver). Fedora :
sudo dnf install -y nvidia-container-toolkit
# 2. Générer la spec CDI (réexécuter après changement de driver/GPU) :
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
# 3. Vérifier l'accès GPU depuis un conteneur :
docker run --rm --device nvidia.com/gpu=0 nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi -L
```

> ⚠️ Utiliser la **syntaxe CDI** `--device nvidia.com/gpu=<index|all>`. Sur certains hôtes,
> `--gpus all` échoue (« failed to discover GPU vendor from CDI / AMD CDI spec not found ») ;
> la forme `--device nvidia.com/gpu=…` est fiable. En compose : `devices: ["nvidia.com/gpu=all"]`.

**Image GPU all-in-one** (`Dockerfile.allinone-gpu`, CUDA 12.6 ; les wheels torch CUDA embarquent
le runtime, le driver vient de l'hôte via CDI) — préférer le quickstart, qui gère build/pull :

```bash
docker compose --profile gpu build       # → transcria-allinone:latest (CUDA 12.6, cu126)
# ou directement : docker build -f Dockerfile.allinone-gpu -t transcria-allinone:latest .
```

> Base CUDA **12.6** (driver ≥ 535, largement répandu). `torch`, `torchaudio` **et `torchcodec`**
> sont installés depuis l'index **cu126** (et non en transitif via PyPI) : `torchcodec` est le
> décodeur audio de pyannote.audio 4.x, couplé à l'ABI/CUDA de torch — un wheel non apparié casse
> l'`AudioDecoder` (diarisation). C'est précisément le pin `torchcodec>=0.12` qui impose cu126/cu130
> (cu128 ne le publie pas). L'image fournit `ffmpeg` (libs FFmpeg requises au runtime par torchcodec).
> Le `Dockerfile` racine reste **CPU** (rôles web/scheduler/migrate).

### All-in-one GPU — tester le projet COMPLET en une commande, sans token

Profil `gpu` du compose : un seul conteneur (UI + scheduler + inférence in-process + **LLM
d'arbitrage embarquée**) + base. Image dédiée `Dockerfile.allinone-gpu`.

```bash
scripts/docker_quickstart.sh               # build/pull + modèles + up + /health (recommandé)
# … ou manuellement :
export POSTGRES_PASSWORD=…
docker compose --profile gpu build         # construit transcria-allinone (CUDA 12.6, llama.cpp)
docker compose --profile gpu run --rm --no-deps all-in-one --provision-only   # tire le GGUF (~6 Go)
docker compose --profile gpu up -d         # db → migrate-gpu → all-in-one
curl -fsS http://localhost:7870/health     # → 200
```

**Ce qui tourne dans le conteneur, sur le GPU** (séquencé par l'autonomie VRAM) :

- **STT** : `whisper` (sans token) ou `cohere` (avec token, qualité de référence) ;
- **Diarisation** : `sortformer` (NVIDIA, sans token, ≤4 locuteurs, expérimental) ou `pyannote`
  (avec token, locuteurs illimités) ;
- **LLM d'arbitrage** (résumé / correction / relecture) : **`llama-server` compilé dans l'image**,
  servant un petit GGUF (palier `TRANSCRIA_LLM_TIER`, défaut 12 Go = Qwen3.5-9B Q5_K_M, **non
  gated**) téléchargé au runtime dans le volume `models`. opencode (agent) est inclus ; son
  `provider.local` est pointé sur `127.0.0.1:8080` au démarrage.

→ **Sans aucun token**, les 6 profils fonctionnent (locuteurs via Sortformer ≤4). Un **token HF
gratuit** (+ conditions des deux modèles) bascule sur la **qualité de référence** (Cohere +
pyannote, locuteurs illimités). Aucun poids n'est dans l'image (build hermétique).

> Le rôle `all` lance le serveur Flask intégré : adapté au **test/démo**, pas à une production à
> fort trafic (préférer alors le split `web` gunicorn + `scheduler`, où la LLM d'arbitrage reste
> un service externe — cf. matrice `TRANSCRIA_ARBITRAGE_LLM_HOST`).
>
> **Pourquoi CUDA 12.6** : le projet épingle `torchcodec>=0.12` (pyannote 4.x), publié sur les
> index torch **cu126** et cu130 (pas cu128). cu126 a le **driver requis le plus répandu (~535+)**.
> **Pourquoi llama.cpp compilé** : llama.cpp ne publie pas de binaire CUDA Linux → on le compile
> dans un étage builder (binaire canonique des paliers).

#### Image `:bundled` — modèles embarqués (zéro-download, hors-ligne)

Variante de l'all-in-one GPU qui **embarque les modèles par défaut non gated** au lieu de les
télécharger au runtime : whisper large-v3 (MIT), Sortformer 4spk (NVIDIA Open Model License), Voxtral Mini 3B (Apache-2.0 — secondaire du multi-STT ciblé, **activé par défaut** depuis 0.3.4), MOSS-Transcribe-Diarize (Apache-2.0 — backend opt-in « une passe ASR+locuteurs », poids **et** site Transformers 5 isolé bakés dans `/opt/transcria-moss-site`, symlinké au démarrage sur le défaut de config), la
LLM d'arbitrage Qwen3.5-9B Q5_K_M (Apache-2.0) **et** le modèle de qualification audio **SQUIM**
(torchaudio, ~29 Mo) — seul modèle que le pipeline téléchargeait encore au runtime (DNSMOS est déjà
un `.onnx` versionné dans le dépôt). Résultat : **aucun téléchargement au 1ᵉ run** (validé E2E).
Image `Dockerfile.allinone-bundled` ; tag `ghcr.io/<owner>/transcria-allinone:bundled`.

```bash
scripts/docker_quickstart.sh --bundled        # pull :bundled si publiée, sinon build local
```

| | `:latest` (slim) | `:bundled` |
|---|---|---|
| Modèles par défaut | téléchargés au 1ᵉ run | **bakés dans l'image** |
| Cache HF | bind du cache **hôte** | **volume nommé** `hfcache` (seedé) |
| 1ᵉ démarrage | réseau requis (~12 Go) | **hors-ligne, instantané** |
| Piège `[Errno 17] File exists` | possible (cache hôte) | **supprimé** |
| Taille image | ~19 Go | ~40 Go |
| `/licenses/` (attributions) | n/a (rien de baké) | **inclus** (NOTICE + NVIDIA OML + MIT) |

Ce n'est **pas** une image « full » : pyannote/Cohere (gated) et les paliers LLM > 12 Go ne sont
**pas** embarqués — ils restent en opt-in (`HF_TOKEN` → cohere+pyannote ; `TRANSCRIA_LLM_TIER` →
GGUF plus gros, téléchargé dans le volume). Les licences de redistribution des modèles bakés ont
été vérifiées (Qwen Apache-2.0, whisper MIT, Sortformer NVIDIA Open Model License, SQUIM CC-BY-4.0 ;
la NVIDIA OML exige de joindre l'accord + l'attribution « Licensed by NVIDIA Corporation under the
NVIDIA Open Model License » : c'est fait dans `/licenses/`).

#### Prérequis GPU / VRAM

**GPU compatibles** : il faut **compute capability ≥ 7.5** **ET ≥ 12 Go** de VRAM (le 9B par défaut
fait ~10,6 Go, cf. table VRAM ci-dessous). `llama-server` embarque le SASS `sm_75→sm_90` + le **PTX
`sm_90`** (vérifié `cuobjdump`) qui JIT vers les archis plus récentes. **Driver NVIDIA ≥ 525**
(CUDA 12.x ; **535+ recommandé** ; Blackwell exige un driver récent). torch cu126 couvre le STT/diar.

| Génération (compute) | Statut | Cartes **≥ 12 Go** (exemples) |
|---|---|---|
| Pascal — GTX 10xx, P40/P100 (6.x) | ❌ non supporté | — |
| Volta — V100, TITAN V (7.0) | ❌ non supporté (`< 7.5`) | — |
| **Turing** (7.5) | ✅ natif | RTX 2060 12G, TITAN RTX 24G, T4 16G, Quadro RTX 6000/8000 |
| **Ampere** (8.0 / 8.6) | ✅ natif | RTX 3060 12G, 3080 12G / 3080 Ti, 3090(Ti) 24G, A10/A40/A100, A5000/A6000 |
| **Ada** (8.9) | ✅ natif | RTX 4070(Ti/Super) 12-16G, 4080 16G, 4090 24G, L4/L40(S), RTX 5000/6000 Ada |
| **Hopper** (9.0) | ✅ natif | H100 80G, H200 141G |
| **Blackwell** (≥ 10.0) | ✅ via **PTX JIT** ¹ | RTX 5070 12G, 5080 16G, 5090 32G, B100/B200 |

> ¹ Blackwell (RTX 50xx, B100/B200) : pas de SASS dédié → JIT du PTX `sm_90` au **1er lancement**
> (plus lent une fois, puis caché). Pour du natif Blackwell, rebâtir l'image avec CUDA 12.8+ et `sm_120`.
> Attention : la plupart des **consumer < 12 Go** (RTX 2070/2080, 3060 Ti, 3070, 4060(Ti), 5060…) sont
> compute-compatibles mais **trop justes en VRAM** pour le 9B — choisir un palier LLM plus petit serait
> nécessaire (non couvert par le défaut).

**VRAM — NON additive** (vérifié sur les logs E2E) : l'autonomie VRAM charge/décharge les modèles
**séquentiellement** — chaque phase réserve puis **libère** le GPU avant la suivante (STT → libéré →
diarisation → libéré → LLM lancée → libérée). Le **pic ≈ la plus grosse phase**, pas la somme.
Empreintes réelles (chemin zéro-token mesuré) :

| Phase | VRAM réelle | Rôle |
|---|---|---|
| **LLM 9B** (palier 12 Go, Q5_K_M) | **~10,6 Go** | maillon dimensionnant |
| Whisper large-v3 (fp16) | < 5 Go | STT |
| Sortformer | ~3,5 Go | diarisation |
| pyannote (référence) | ~2 Go | diarisation (avec token) |

→ **Un seul GPU ~12 Go** (Turing 7.5+) suffit pour le workflow complet : le 9B (phase la plus
lourde) est chargé **après** libération du STT/diar. Le prix du non-additif : recharger les modèles
entre phases est **plus lent** (pas de co-résidence). **16 Go+ confortable** ; un palier LLM
supérieur ou la qualité de référence (cohere ~6 Go + pyannote) demandent davantage / multi-GPU.

> Le quickstart **aligne automatiquement** `gpu.llm_vram_mb` sur le palier (12 Go → `12000`) — sinon
> le défaut `60000` (palier 64 Go) ferait **refuser l'admission** du 9B sur une carte 12-24 Go.

### Publication d'une image publique (GHCR)

L'image all-in-one GPU est **publiable** : licences permissives (projet Apache-2.0, llama.cpp
MIT, NeMo Apache-2.0, opencode MIT, torch BSD, base CUDA redistribuable) et **aucun poids
embarqué**. Le workflow `.github/workflows/publish-image.yml` construit et pousse
`ghcr.io/<owner>/transcria-allinone:<tag>` (+ `:latest`) sur push d'un tag `v*` ou via
`workflow_dispatch`.

Côté testeur, pointer le quickstart sur l'image publiée fait un **`pull`** (pas de build) :

```bash
TRANSCRIA_ALLINONE_IMAGE=ghcr.io/<owner>/transcria-allinone:vX.Y.Z scripts/docker_quickstart.sh
```

> **Driver minimum** : CUDA 12.6 → driver NVIDIA **≥ 535** (Linux). Si le driver est plus ancien,
> le quickstart retombe sur un **build local**. L'image est volumineuse (**~19 Go** : base CUDA
> devel + torch + NeMo) ; le build CI est lourd, le workflow
> libère l'espace disque du runner ; sinon builder/pousser depuis une machine GPU locale.
>
> *Validé E2E (2026-06-23, 8× RTX 3090) : pipeline complet, qualité 97/100, livrables SRT/ZIP/DOCX.*

#### Publication de l'image `:bundled` (build local + push manuel)

L'image `:bundled` (~40 Go avec les poids) **dépasse le disque d'un runner GitHub standard** → elle
n'est **pas** construite en CI (le workflow `publish-image.yml` ne publie que le slim). On la
construit et la pousse **depuis une machine GPU** :

```bash
# 1. Construire (télécharge ~21 Go de poids NON gated au build — réseau requis, aucun token) :
docker build -f Dockerfile.allinone-bundled -t ghcr.io/<owner>/transcria-allinone:bundled .
# 2. S'authentifier sur GHCR (token avec scope write:packages) puis pousser :
echo "$GHCR_TOKEN" | docker login ghcr.io -u <owner> --password-stdin
docker push ghcr.io/<owner>/transcria-allinone:bundled
# 3. Rendre le package PUBLIC une fois (Settings → Packages → Change visibility).
```

Côté testeur ensuite : `scripts/docker_quickstart.sh --bundled` fait un simple **`pull`**.

### Nœud de ressources GPU séparé (déploiement split)

```bash
docker run -d --device nvidia.com/gpu=0 -e TRANSCRIA_ROLE=resource-node \
    -v $PWD/config.yaml:/app/config.yaml:ro -v $PWD/.env:/app/.env:ro \
    -v $PWD/models:/app/models -p 8002:8002 transcria:latest
```

`resource-node` n'exige pas de base applicative ; il expose `/capabilities` (qui énumère les
GPU vus par le conteneur) et `/engines/ensure`. Le scheduler le référence via
`inference.mode=remote`.

### Banc split GPU complet avec vLLM (STT Cohere + LLM d'arbitrage)

Pour un déploiement split **entièrement containerisé** où le nœud GPU sert AUSSI le STT et
le LLM d'arbitrage via **vLLM** (au lieu de services externes), un banc dédié est fourni :
`docker-compose.split-gpu.yml` + `config.split.example.yaml`. Référence détaillée (décisions,
risques, placement VRAM, FP8 sur Ampere) : **[docs/PLAN_TEST_SPLIT_VLLM.md](PLAN_TEST_SPLIT_VLLM.md)**.

Particularités vs le `docker run` minimal ci-dessus :

- **Images construites via `install.sh`** (on teste l'install comme un utilisateur), **builds
  hermétiques — aucune base PostgreSQL requise** : le worker passe par `install.sh --pg-defer`
  (écrit le DSN sans se connecter ; le schéma est appliqué au runtime par le job `migrate`).
  ```bash
  docker build -f Dockerfile.worker        -t transcria-worker:latest .
  docker build -f Dockerfile.resource-node -t transcria-resource-node:latest .   # base CUDA + venv vLLM
  ```
  Le worker embarque opencode (installé par `install.sh`, profil `scheduler`) ; le nœud ajoute
  un **venv vLLM isolé** (`/opt/vllm-venv`) à côté du venv projet (torch cu130) — les deux piles
  torch ne se mélangent pas.
- **opencode** est installé au build, et son `provider.local` est **reconfiguré au démarrage**
  (entrypoint) depuis la config montée → il pointe sur le vLLM d'arbitrage du nœud. L'**image de
  base** (`Dockerfile`, profils `scheduler`/all-in-one) installe désormais elle aussi opencode via
  l'installateur officiel (elle est construite par `pip install`, pas par `install.sh`) : les rôles
  qui exécutent les phases LLM en disposent quelle que soit la topologie.
- **STT Cohere** servi par vLLM dans le nœud (`/engines/ensure` lance `launch_stt_cohere.sh`,
  `STT_BIN` = venv vLLM) ; **LLM d'arbitrage** = service `vllm-arbitrage` (Qwen3.6-27B-FP8, TP=4,
  FP8 Marlin sur Ampere) via `scripts/launch_arbitrage_vllm.sh`.
- Les **8 GPU** sont exposés (`nvidia.com/gpu=all`) : le code d'autonomie VRAM place arbitrage
  (TP=4) + STT + diarisation (`device: auto`).

```bash
# 1. Préparer config.yaml (fusionner config.split.example.yaml) ; télécharger le modèle FP8 (~27 Go)
#    dans ./models ou le cache HF ; accepter les conditions Cohere (modèle gaté).
# 2. Lancer le banc :
POSTGRES_PASSWORD=… TRANSCRIA_INFERENCE_API_KEY=… HF_TOKEN=hf_… \
  docker compose -f docker-compose.split-gpu.yml up -d
# 3. Vérifier de bout en bout (plan de contrôle + job son réel). Le service `verify` a pour
#    entrypoint verify_split_topology.py ; on lui passe les URLs (réseau compose) + l'audio :
docker compose -f docker-compose.split-gpu.yml run --rm verify \
  --web http://web:7870 --node http://resource-node:8002 --arbitrage http://vllm-arbitrage:8080 \
  --audio /app/tests/test2.mp3 --password "$ADMIN_PASSWORD"
```

## Procédure de rollback

- **Rollback de code SANS changement de schéma** (les deux versions partagent la même
  révision Alembic) : redéployer **uniquement les services applicatifs** avec `--no-deps`
  pour **ne pas rejouer `migrate`** (un `up` normal le relancerait, car `migrate` est hors
  profil) :
  ```bash
  docker compose --profile split stop web scheduler
  TRANSCRIA_IMAGE=transcria:<tag-précédent> \
    docker compose --profile split up -d --no-deps web scheduler
  # tout-en-un : --profile gpu … --no-deps all-in-one
  ```
- **Rollback à travers une migration de schéma** : un `migrate` de l'**ancienne** image
  échouerait sur une révision inconnue (et les données peuvent être incompatibles).
  Procédure sûre : **restaurer la sauvegarde PostgreSQL** compatible (`pg_restore`) prise
  avant la montée de version, *puis* redéployer l'ancienne image. **Conserver un `pg_dump`
  avant chaque `migrate`.**
- ⚠️ **L'image cible doit exister.** `TRANSCRIA_IMAGE=transcria:<tag>` ne déclenche un vrai
  rollback que si cette image est présente localement (ou tirée d'un registre). Sinon, comme
  un `build:` est défini, Compose **reconstruit le code courant** sous cet ancien tag — faux
  rollback. Garder les images des versions déployées (ou les publier sur un registre, cf.
  backlog 0.x), idéalement référencées par digest en production.
- **Compatibilité du manifeste** : le rollback réutilise le `docker-compose.yml` et le
  `config.yaml` du checkout **courant** avec une **ancienne** image. Il suppose donc que les
  contrats n'ont pas changé entre les versions (noms de rôles, commandes d'entrypoint,
  variables d'environnement, chemins de volumes, format de `config.yaml`). Pour un rollback
  pleinement reproductible, versionner **ensemble** : image + `docker-compose.yml` + `config.yaml`
  + révision Alembic associée (cf. backlog 0.x : images immuables sur registre).
- **Données de jobs** : les volumes `jobs`/`models` persistent indépendamment des
  conteneurs ; un rollback de code ne les touche pas.

## Volumes

| Volume | Monté dans | Contenu |
|---|---|---|
| `pgdata` | `db` | Données PostgreSQL |
| `jobs` | `web` + `scheduler` (split), `all-in-one` (gpu) | Espaces de travail des jobs — **volume partagé** entre web et scheduler (mono-hôte → `shared_backend: fs` suffit) |
| `models` | `web` + `scheduler` (split), `all-in-one` (gpu) | Modèles/caches locaux |
| `hfcache` | `all-in-one` (gpu, **mode `:bundled`** uniquement) | Cache HF seedé depuis l'image (`TRANSCRIA_HF_SOURCE=hfcache`) ; remplace le bind du cache hôte |
| `./config.yaml` (bind, ro) | tous | Configuration applicative |
| `./.env` (bind, ro) | tous | Secrets (clé Flask, clés API…) |

## Statut de validation

Vérifié réellement (build + run) sur Fedora 42, Docker 29, 8× RTX 3090, driver 580 :

- ✅ **CPU** : `migrate` (3 migrations Alembic en conteneur, exit 0), `web` (gunicorn, `/health` 200).
- ✅ **GPU** : image CUDA (torch 2.12+cu130), `torch.cuda` + matmul GPU dans le conteneur ;
  rôle `resource-node` (gunicorn `inference_service`, `/health` 200, `/capabilities` énumère la RTX 3090) ;
  **tout-en-un `--profile gpu`** (`/health` 200, GPU vu) via CDI.
- ✅ **E2E réel** (`tests/test_e2e_workflow.py --audio tests/test2.mp3 --mode quality --skip-llm`,
  HF_TOKEN + cache monté) : STT Cohere + diarisation pyannote **sur GPU en conteneur** →
  **29 segments, 2 locuteurs, SRT 2630 c., score qualité 97/100**. La transcription et la
  diarisation du pipeline fonctionnent intégralement en conteneur.

Non couvert / dépendances externes :

- **Correction/résumé LLM** : non joués ici (`--skip-llm`) — nécessitent un LLM
  OpenAI-compatible joignable. **LLM d'arbitrage** = service externe (non conteneurisé par ce compose).
- Reverse-proxy TLS (nginx) : voir `deploy/nginx-transcria.conf.example`.

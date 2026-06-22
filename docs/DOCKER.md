# Déploiement Docker (P5)

> Référence du déploiement conteneurisé de TranscrIA. Suit les invariants de
> `docs/archive/PLAN_EVOLUTION_INSTALLATION.md § P5`. Les images applicatives (`web`,
> `scheduler`, `migrate`) sont construites par le `Dockerfile` à la racine et
> orchestrées par `docker-compose.yml`.

## Démarrage rapide (une commande)

De `git clone` à un conteneur qui tourne, sans étape manuelle — `scripts/docker_quickstart.sh`
orchestre tout (prérequis GPU, génération `.env`/`config.yaml`, build avec le bon index
CUDA, `compose up`, vérification `/health`) :

```bash
# Tout-en-un GPU (recommandé pour tester le projet) :
scripts/docker_quickstart.sh                  # → http://localhost:7870 (admin / cf. config.yaml)

# Avec le STT de référence (Cohere, gated) — fournir un token HF :
HF_TOKEN=hf_xxx scripts/docker_quickstart.sh

# Sans GPU (web + scheduler, pas d'inférence locale) :
scripts/docker_quickstart.sh --cpu

# Arrêt :
scripts/docker_quickstart.sh --down
```

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

**2. Modèles STT/diarisation** — deux chemins selon le besoin :

| Besoin | Backend | Token HF |
|---|---|---|
| **Test rapide, sans friction** | `models.stt_backend: "whisper"` (openai/whisper-large-v3, non gated) | ❌ aucun |
| **Qualité de référence (prod)** | `models.stt_backend: "cohere"` (CohereLabs, **gated**) | ✅ requis |

Pour Cohere (gated) : (a) accepter les conditions du modèle sur
`huggingface.co/CohereLabs/cohere-transcribe-03-2026`, (b) créer un token HF, (c) le
fournir au conteneur (`HF_TOKEN`, ou dans `.env`). La diarisation `pyannote` est
également gated → même token. Le cache HF de l'hôte est monté dans le conteneur (volume
`/hf`) pour éviter de re-télécharger.

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
- **LLM d'arbitrage** — service externe OpenAI-compatible (recommandé) ou conteneur
  dédié ; jamais embarqué dans l'image applicative.

## Matrice des variables

| Variable | Rôles | Obligatoire | Description |
|---|---|---|---|
| `TRANSCRIA_ROLE` | tous | oui (ou argument) | `web` \| `scheduler` \| `resource-node` \| `migrate` |
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

Build-time (`docker build --build-arg`) :

| Arg | Défaut | Description |
|---|---|---|
| `PYTHON_VERSION` | `3.11` | Version de l'image de base Python |
| `TORCH_INDEX_URL` | `…/whl/cpu` | Index des wheels PyTorch (CPU). Image GPU : index CUDA. |

## Procédure de démarrage

1. **Préparer la configuration** (non versionnée, montée au runtime) :
   ```bash
   ./install.sh --profile web --non-interactive --skip-deps --no-service \
       --postgres --pg-existing --pg-host db --pg-user transcria --pg-db transcria --pg-password "$POSTGRES_PASSWORD"
   # ⇒ produit config.yaml + .env localement (à monter)
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

**Image GPU** (les wheels torch CUDA embarquent le runtime ; le driver vient de l'hôte via CDI) :

```bash
docker build --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu130 -t transcria:latest .
```

> L'index `cu130` correspond au driver récent (≥ 580) de cette plateforme ; adapter à votre
> version de driver/CUDA (`cu124`, `cu126`, …). Vérifié : torch 2.12 + cu130, `torch.cuda.is_available()`
> True dans le conteneur, RTX 3090 énumérée par `/capabilities`.
>
> `torch`, `torchaudio` **et `torchcodec`** sont installés depuis cet index (et non en transitif
> via PyPI) : `torchcodec` est le décodeur audio de pyannote.audio 4.x, couplé à l'ABI/CUDA de
> torch — un wheel non apparié casse l'`AudioDecoder` (diarisation). L'image fournit `ffmpeg`
> (libs FFmpeg requises au runtime par torchcodec).

### Option simple pour tester le projet — tout-en-un GPU (une commande)

Profil `gpu` du compose : un seul conteneur (UI + scheduler + inférence in-process) + base.

```bash
docker build --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu130 -t transcria:latest .
export POSTGRES_PASSWORD=…
docker compose --profile gpu up -d        # db → migrate → all-in-one (GPU)
curl -fsS http://localhost:7870/health    # → 200
```

> Le rôle `all` lance le serveur Flask intégré (comme l'all-in-one natif) : adapté à un
> **déploiement de test/démo**, pas à une production à fort trafic (préférer alors le split
> `web` gunicorn + `scheduler`). Un GPU est requis pour le traitement réel des jobs (STT,
> diarisation) ; le conteneur démarre et sert l'UI même sans modèles (chargés à la demande).
> La **LLM d'arbitrage** reste une dépendance externe (service OpenAI-compatible) — l'étape
> de correction peut être désactivée (`arbitration_llm.enabled: false`) pour un test sans LLM.

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

- **Images construites via `install.sh`** (on teste l'install comme un utilisateur) :
  ```bash
  docker build -f Dockerfile.worker        -t transcria-worker:latest .
  docker build -f Dockerfile.resource-node -t transcria-resource-node:latest .   # base CUDA + venv vLLM
  ```
  Le worker embarque opencode (installé par `install.sh`, profil `scheduler`) ; le nœud ajoute
  un **venv vLLM isolé** (`/opt/vllm-venv`) à côté du venv projet (torch cu130) — les deux piles
  torch ne se mélangent pas.
- **opencode** est installé au build, et son `provider.local` est **reconfiguré au démarrage**
  (entrypoint) depuis la config montée → il pointe sur le vLLM d'arbitrage du nœud. *(Ceci corrige
  aussi l'all-in-one : opencode n'était présent dans aucune image avant.)*
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

# Déploiement Docker (P5)

> Référence du déploiement conteneurisé de TranscrIA. Suit les invariants de
> `docs/PLAN_EVOLUTION_INSTALLATION.md § P5`. Les images applicatives (`web`,
> `scheduler`, `migrate`) sont construites par le `Dockerfile` à la racine et
> orchestrées par `docker-compose.yml`.

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
| `TRANSCRIA_SECRET` | web, scheduler | oui (via `.env`) | Clé Flask (dans `.env` monté) |

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
3. **Démarrer** :
   ```bash
   docker compose up -d --build
   ```
   `db` → healthy → `migrate` (one-shot) → `web` + `scheduler`.
4. **Vérifier** :
   ```bash
   docker compose ps
   docker compose logs -f migrate     # doit afficher "alembic upgrade head" puis sortir 0
   curl -fsS http://localhost:7870/health
   ```

### Nœud de ressources (GPU)

Image distincte (base CUDA) sur l'hôte GPU :

```bash
docker build --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu124 -t transcria-gpu .
docker run --gpus all -e TRANSCRIA_ROLE=resource-node \
    -v $PWD/config.yaml:/app/config.yaml:ro -v $PWD/.env:/app/.env:ro \
    -p 8002:8002 transcria-gpu
```

`resource-node` n'exige pas de base applicative ; il expose `/capabilities` et
`/engines/ensure`. Le scheduler le référence via `inference.mode=remote`.

## Procédure de rollback

- **Code applicatif** : redéployer le tag d'image précédent
  ```bash
  docker compose down
  TRANSCRIA_IMAGE=transcria:<tag-précédent> docker compose up -d   # (épingler l'image)
  ```
  Aucune migration n'est jouée par les serveurs : tant que `migrate` n'est pas relancé,
  le schéma reste celui en place.
- **Schéma de base** : Alembic est en avant uniquement ; pour revenir en arrière,
  restaurer une sauvegarde PostgreSQL (`pg_dump`/`pg_restore`) prise avant la montée de
  version, puis redéployer l'image compatible. Conserver un dump avant chaque `migrate`.
- **Données de jobs** : les volumes `jobs`/`models` persistent indépendamment des
  conteneurs ; un rollback de code ne les touche pas.

## Volumes

| Volume | Monté dans | Contenu |
|---|---|---|
| `pgdata` | `db` | Données PostgreSQL |
| `jobs` | `scheduler` | Espaces de travail des jobs |
| `models` | `scheduler` | Modèles/caches locaux |
| `./config.yaml` (bind, ro) | tous | Configuration applicative |
| `./.env` (bind, ro) | tous | Secrets (clé Flask, clés API…) |

## Limites connues / non couvert ici

- Le `docker build` complet tire PyTorch (lourd) ; le `Dockerfile` et le compose sont
  fournis comme **référence validée au niveau schéma** (`docker compose config`). Un
  `docker build` / `compose up` réel sur la cible reste l'étape de validation finale.
- Reverse-proxy TLS (nginx) : voir `deploy/nginx-transcria.conf.example`.

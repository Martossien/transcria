# Docker Deployment (P5)

> 🇫🇷 Version française : [DOCKER.md](DOCKER.md) — the French version is the canonical reference; if they ever disagree, the French one is right.

> Reference for TranscrIA's containerized deployment (original invariants:
> `docs/archive/PLAN_EVOLUTION_INSTALLATION.md § P5` (French), archived). Three families of
> application images:
>
> - **Distributed CPU roles** (`web`, `scheduler`, `migrate`): a single lightweight image built
>   by the root `Dockerfile` (CPU torch) and orchestrated by `docker-compose.yml` (`split` profile).
> - **All-in-one GPU** (`all`): a **dedicated** image `Dockerfile.allinone-gpu` (CUDA 12.8 base — native sm_120/RTX 50xx,
>   **compiled llama.cpp** = embedded arbitration LLM, **NeMo/Sortformer** for non-gated
>   diarization), `gpu` profile. It delivers the **complete GPU workflow in one command, no token
>   required** — see "All-in-one GPU" below. **No model weights embedded** (downloaded at
>   runtime) → the image is **publishable** (see § Publishing).
> - **All-in-one GPU bundled** (`Dockerfile.allinone-bundled`): the same, plus **three NON-gated
>   models baked in** (whisper + Sortformer + Qwen) → zero download, offline; local build
>   ~31 GB, published by `scripts/release_bundled.sh` only — see § slim vs bundled.
>
> All of them ship opencode (the agent for LLM phases). On top of these come the split bench
> infrastructure images (`Dockerfile.resource-node` for the GPU node, `Dockerfile.worker` — built
> by running `install.sh`) and the **meeting bot** images, never built by hand
> (`scripts/bot.sh` takes care of it): `Dockerfile.bot` (browser), `Dockerfile.visio` (native
> LiveKit client), `Dockerfile.zoom-sdk` (Zoom SDK) — see `docs/BOT_REUNION.md` (French).

## Quick start (one command)

From `git clone` to a running container, with no manual step — `scripts/docker_quickstart.sh`
orchestrates everything (GPU prerequisites, `.env`/`config.yaml` generation, build with the
right CUDA index, `compose up`, `/health` check):

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

> **Two GPU images.** `:latest` (**slim**) downloads the models on first run into the host HF
> cache. `:bundled` (`--bundled`) **embeds** whisper + Sortformer + Voxtral + MOSS-TD (one-pass
> ASR+speakers, opt-in) + the 9B LLM → no download, HF cache in a **named volume** seeded
> from the image (eliminates the `[Errno 17] File exists` below). In both cases,
> pyannote/Cohere remain opt-in via `HF_TOKEN` (and Kroko-ASR via the "Models" page).
>
> **Served STT runtimes in the GPU images (0.3.6).** The `:latest`/`:bundled` and
> `resource-node` images ship the PINNED binaries of audio.cpp (`qwen3asr`) and parakeet.cpp
> (`nemotron`) under `/opt/runtimes` (`TRANSCRIA_RUNTIMES_DIR`). In `:latest`/`resource-node`,
> the models remain **volume-based**: `hf download Qwen/Qwen3-ASR-1.7B-hf` (plain snapshot) and
> the Nemotron GGUF via the "Models" page — point the launcher's `STT_MODEL` at the mounted
> path. **Since 0.3.8, `:bundled` ALSO embeds the Qwen3-ASR-1.7B weights** at the launcher's
> default path (`/opt/runtimes/audiocpp/src/models/Qwen3-ASR-1.7B-hf`):
> `scripts/launch_stt_qwen3asr.sh` works with no variable, and
> `models.summary_stt_backend: qwen3asr` (summary phase ×2.4, best quality on the bench)
> is "pull & run". Configuration: `docs/EXTERNAL_STT_RUNTIMES.md` (French).
>
> **Upgrading the LLM tier from the `:bundled` image.** The image embeds the 12 GB tier
> (Qwen3.5-9B) **and the 8 GB one** (Qwen3.5-4B — picked automatically on cards < 12 GB),
> but `MODELS_DIR` (the `models` volume) and `/hf` are **writable volumes**:
> setting `TRANSCRIA_LLM_TIER=16|24|32|48|64` (or downloading from **Administration → Models**)
> **downloads the bigger LLM at runtime** into the persistent volume — the baked model does
> not prevent the upgrade.

> The quickstart **checks the GPU** before anything else (compute capability ≥ 7.5; VRAM ≥ ~8 GB —
> slim and bundled alike since 0.4.2, since both the 8 and 12 GB LLM tiers are baked in,
> see `transcria.deploy.gpu_preflight`) and fails early with a clear message rather than letting
> a CUDA crash happen on the first job.

> **First login**: open `http://localhost:7870` — on the **first visit** (empty database),
> the portal asks you to **create the administrator account** (username + password of your
> choosing), whatever the path (quickstart or manual compose). Automation:
> setting `auth.first_admin_password` in `config.yaml` **before the first start** creates
> the account at boot, with no page; changing it **afterwards** does NOT change the password of an
> already-created account (do that via the UI instead). Password lost later:
> `docker compose exec <service-web> python -m transcria.maintenance.cli reset-admin-password admin`.

The script is **idempotent**: it does not overwrite an existing `config.yaml`/`.env`, generates
random secrets, and picks `whisper` (non-gated, no token) when `HF_TOKEN` is absent.
The sections below detail each step for manual control.

## Prerequisites (what a user has to do)

> **Docker version: ≥ 25 required** for the documented GPU path (CDI) — the
> `docker.io` 20.10 from the Ubuntu/Debian repositories is not enough (install Docker CE).
> Constrained hosts (old daemon, nested LXC where runc ≥ 1.3 is blocked): legacy mode
> works — a compose override replacing the CDI `devices` with
> `runtime: nvidia` (`devices: !reset []` + `NVIDIA_VISIBLE_DEVICES: all`, with
> `no-cgroups = true` in `/etc/nvidia-container-runtime/config.toml` under LXC).

**1. GPU access inside Docker** — is NOT handled by `requirements.txt` (Python dependencies)
nor by `install.sh` (native install): it is Docker-host configuration, isolated in a
dedicated, idempotent script:

```bash
scripts/setup_docker_gpu.sh          # installe nvidia-container-toolkit + génère la spec CDI + vérifie
scripts/setup_docker_gpu.sh --check  # vérifie seulement (GPU visible en conteneur ?)
```

> Script prerequisites: NVIDIA driver (`nvidia-smi`) + Docker already installed (it does not touch them).
> It makes the GPU visible via **CDI** (`--device nvidia.com/gpu=…`).

**2. STT/diarization models** — two paths depending on the need. **Without a token, EVERYTHING works**
(transcription + speakers + summary/correction) with **non-gated** models; the HF token
only buys **reference quality**:

| Need | STT | Diarization | HF token |
|---|---|---|---|
| **Quick test, zero friction** | `whisper` (openai/whisper-large-v3, non-gated) | `sortformer` (NVIDIA, non-gated, **≤4 speakers**, experimental) | ❌ none |
| **Reference quality (prod)** | `cohere` (CohereLabs, **gated**) | `pyannote` (**gated**, unlimited speakers) | ✅ required |

The quickstart automatically picks the first row without `HF_TOKEN`, the second with it. For
reference quality: (a) accept the terms of **BOTH** models on
`huggingface.co/CohereLabs/cohere-transcribe-03-2026` **and**
`huggingface.co/pyannote/speaker-diarization-community-1`, (b) create an HF token, (c) provide
it to the container (`HF_TOKEN`, or in `.env`). The host's HF cache is mounted into the container (volume
`/hf`) to avoid re-downloading.

> ⚠️ **Host cache pre-populated by ANOTHER user.** The container runs as **root**; if
> `HF_CACHE_DIR` points at a cache already populated by a non-root user (symlinks/permissions),
> loading **faster-whisper** can fail (`[Errno 17] File exists`) → empty transcription.
> For a **fresh** user (empty cache), whisper and Sortformer download cleanly **without a
> token** (validated E2E). If you hit trouble, point `HF_CACHE_DIR` at a **container-dedicated
> cache** (empty directory) rather than reusing a heterogeneous host cache.
> **The `:bundled` image (`--bundled`) removes this trap**: it does not use the host cache (HF
> cache in a named volume `hfcache`, seeded from the baked models).

> ⚠️ `transcria.stt.cohere_transcriber` forces `HF_HUB_OFFLINE=1` by default. In a container
> with a freshly mounted cache, leave **`HF_HUB_OFFLINE=0`** (the compose does this) so
> that the first resolution of the gated model succeeds; after that the cache serves the weights.

## Principles

- **`install.sh` is never the application entrypoint.** The image is built once;
  at runtime, the entrypoint `python -m transcria.deploy.entrypoint <role>`
  validates the invariants, waits for the database, then **replaces the process** with the
  role's server. Installation logic stays in `transcria.installer` (reused outside
  containers by `install.sh`).
- **Same profiles as the install**: `web`, `scheduler`, `resource-node`, `migrate`.
- **PostgreSQL is mandatory.** SQLite is not a supported Docker deployment mode;
  the entrypoint refuses to start a role that needs the application database without a PostgreSQL DSN.
- **`migrate` is a one-shot job** (`alembic upgrade head`): the servers do not auto-migrate,
  they wait for the dedicated migration to have succeeded.
- **No secret baked into the image**: `config.yaml` and `.env` are provided via volumes;
  the DSN via `TRANSCRIA_DATABASE_URL`.

## Target layout

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

Containers **external** to this compose:

- **resource-node** (GPU) — local STT/diarization/voice. CUDA-based image (see
  below), deployed on the GPU host; declared on the scheduler side via
  `inference.mode=remote` + node URLs.
- **Arbitration LLM** — **embedded in the all-in-one GPU** (`Dockerfile.allinone-gpu`:
  compiled llama.cpp + small GGUF pulled at runtime), launched on demand by the VRAM autonomy.
  For the **CPU/split roles** (`web`/`scheduler`), it remains **external**: an
  OpenAI-compatible service or a dedicated container, host/port resolved in a single place
  (`services.arbitrage_llm_host`/`arbitrage_llm_port`, overridable via
  `TRANSCRIA_ARBITRAGE_LLM_HOST` — e.g. `host.docker.internal` + `extra_hosts: host-gateway`).

## Variable matrix

| Variable | Roles | Required | Description |
|---|---|---|---|
| `TRANSCRIA_ROLE` | all | yes (or argument) | `web` \| `scheduler` \| `resource-node` \| `migrate` \| `all` (all-in-one) |
| `TRANSCRIA_DATABASE_URL` | web, scheduler, migrate | **yes** | PostgreSQL DSN (`postgresql+psycopg://…`). SQLite refused. |
| `TRANSCRIA_CONFIG` | all | no (default `/app/config.yaml`) | Path of the mounted `config.yaml` |
| `TRANSCRIA_BIND` | web | no (default `0.0.0.0:7870`) | gunicorn listen address |
| `TRANSCRIA_WORKERS` | web | no (default `4`) | gunicorn workers |
| `INFERENCE_BIND` / `INFERENCE_PORT` | resource-node | no (default `0.0.0.0:8002`) | Inference service listen address |
| `INFERENCE_THREADS` | resource-node | no (default `4`) | Node's gunicorn threads |
| `POSTGRES_PASSWORD` | db (+ DSN) | **yes** | Password of the `transcria` role (compose) |
| `TRANSCRIA_SECRET` | web, scheduler, all | yes (via `.env`) | Flask key (in the mounted `.env`) |
| `HF_TOKEN` | all, resource-node (GPU) | if gated STT/diar (Cohere/pyannote) | Hugging Face token (gated models) |
| `HF_CACHE_DIR` | all (compose) | no (default `~/.cache/huggingface`) | Host HF cache mounted into `/hf` |
| `HF_HUB_OFFLINE` | all, resource-node | no (compose sets `0`) | `0` required for the first resolution of a gated model in a container |
| `TRANSCRIA_ARBITRAGE_LLM_HOST` | scheduler, all | no (default `services.arbitrage_llm_host` or `127.0.0.1`) | Arbitration LLM host. Override shared by the `vram_manager` probe AND the opencode provider (single resolution) — useful when the LLM runs on the host/a node (e.g. `host.docker.internal`) |
| `TRANSCRIA_LLM_TIER` | all | no (default `12`) | VRAM tier of the embedded LLM (12/16/24/32/48/64). Drives the downloaded GGUF AND the tier's launch script (`scripts/arbitrage_profiles/<tier>gb_*.sh`) |
| `MODELS_DIR` | all | no (default `/app/models`) | Directory (`models` volume, persistent) where the arbitration GGUF is downloaded at runtime |
| `TRANSCRIA_ARBITRAGE_SCRIPT` | all | no (derived from the tier) | Explicit override of the LLM launch script (otherwise resolved from `TRANSCRIA_LLM_TIER`) |
| `TRANSCRIA_DEFAULT_LOCALE` | all, web | no (default `fr`) | Default interface language (`fr`/`en`) — overrides `i18n.default_locale` without editing the YAML. The navbar selector and the per-user preference remain available; the language of the deliverables is set per job |
| `TRANSCRIA_ALLINONE_IMAGE` | all (compose) | no (default `transcria-allinone:latest`) | GPU image ref. Point it at a registry tag (e.g. `ghcr.io/<owner>/transcria-allinone:vX`) → the quickstart does a `pull` instead of a build |

Build-time (`docker build --build-arg`):

| Arg | Default | Description |
|---|---|---|
| `PYTHON_VERSION` | `3.11` | Version of the Python base image |
| `TORCH_INDEX_URL` | `…/whl/cpu` | PyTorch wheels index (CPU). GPU image: CUDA index. |

## Startup procedure

1. **Prepare the configuration** (not versioned, mounted at runtime):
   ```bash
   ./install.sh --profile web --non-interactive --skip-deps --no-service \
       --postgres --pg-defer --pg-host db --pg-user transcria --pg-db transcria --pg-password "$POSTGRES_PASSWORD"
   # ⇒ produit config.yaml + .env localement (à monter). `--pg-defer` écrit le DSN SANS se
   #    connecter : `db` n'est pas résoluble depuis l'hôte et la base n'est pas encore démarrée ;
   #    le schéma est appliqué au runtime par le job `migrate`.
   ```
   or generate `config.yaml` via `scripts/bootstrap_config.py --profile web` then fill in `.env`.
2. **Export the database secret**: `export POSTGRES_PASSWORD=…`
3. **Start** (`split` profile = web + scheduler; `db`/`migrate` are outside the profile):
   ```bash
   docker compose --profile split up -d --build
   ```
   `db` → healthy → `migrate` (one-shot) → `web` + `scheduler`.

   > The `split` (web+scheduler) and `gpu` (all-in-one) profiles are **alternatives — do not
   > enable them together**: both publish `:7870` (Compose technically allows both, but it
   > would be a port conflict). `db`/`migrate` start in both cases.
4. **Check**:
   ```bash
   docker compose ps
   docker compose logs -f migrate     # doit afficher "alembic upgrade head" puis sortir 0
   curl -fsS http://localhost:7870/health
   ```

### Troubleshooting — `migrate` fails while `db` is *healthy*

`POSTGRES_PASSWORD` is only applied at PostgreSQL **volume initialization**. If a **pre-existing**
data volume was created with a different password, the database keeps the old one and
TCP authentication fails — even though the service responds. `migrate` now shows the **real
cause** instead of a misleading "unreachable":

```
[ERROR] base PostgreSQL inaccessible après 30 tentatives — AUTHENTIFICATION refusée (mot de passe)…
```

Three remedies, depending on whether you want to keep the data:
- **reuse** the original password in `POSTGRES_PASSWORD`; or
- **reset** the volume (⚠ erases the data): `docker compose down -v` then start again; or
- **change** the role's password without losing the data:
  `docker compose exec db psql -U transcria -d transcria -c "ALTER USER transcria WITH PASSWORD '<nouveau>';"`.

## GPU (validated)

GPU in Docker goes through **CDI** (Container Device Interface). Host setup, once:

```bash
# 1. Toolkit conteneur NVIDIA (ne touche pas le driver). Fedora :
sudo dnf install -y nvidia-container-toolkit
# 2. Générer la spec CDI (réexécuter après changement de driver/GPU) :
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
# 3. Vérifier l'accès GPU depuis un conteneur :
docker run --rm --device nvidia.com/gpu=0 nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi -L
```

> ⚠️ Use the **CDI syntax** `--device nvidia.com/gpu=<index|all>`. On some hosts,
> `--gpus all` fails ("failed to discover GPU vendor from CDI / AMD CDI spec not found");
> the `--device nvidia.com/gpu=…` form is reliable. In compose: `devices: ["nvidia.com/gpu=all"]`.

**All-in-one GPU image** (`Dockerfile.allinone-gpu`, CUDA 12.8 + torch cu130; the CUDA torch wheels ship
the runtime, the driver comes from the host via CDI) — prefer the quickstart, which handles build/pull:

```bash
docker compose --profile gpu build       # → transcria-allinone:latest (CUDA 12.8, cu130)
# ou directement : docker build -f Dockerfile.allinone-gpu -t transcria-allinone:latest .
```

> CUDA **12.8** base + torch **cu130** index since 0.4.4 (support for **RTX 50xx / Blackwell,
> native sm_120** — same pair as the resource-node image). **BREAKING: NVIDIA driver ≥ 580
> required** (CUDA 13 wheels) — older driver: stay on the 0.4.3 images or update the
> driver. `torch`, `torchaudio` **and `torchcodec`** are installed from the cu130 index
> (not transitively via PyPI): `torchcodec` is the audio decoder of
> pyannote.audio 4.x, coupled to torch's ABI/CUDA — a mismatched wheel breaks the
> `AudioDecoder` (diarization); the `torchcodec>=0.12` pin is only published on cu126 and
> cu130 (not cu128), and only cu130 carries the sm_120 kernels. The image provides `ffmpeg`
> (FFmpeg libs required at runtime by torchcodec). The root `Dockerfile` remains **CPU**
> (web/scheduler/migrate roles).

### All-in-one GPU — test the COMPLETE project in one command, no token

The compose's `gpu` profile: a single container (UI + scheduler + in-process inference +
**embedded arbitration LLM**) + database. Dedicated image `Dockerfile.allinone-gpu`.

```bash
scripts/docker_quickstart.sh               # build/pull + modèles + up + /health (recommandé)
# … ou manuellement :
export POSTGRES_PASSWORD=…
docker compose --profile gpu build         # construit transcria-allinone (CUDA 12.8, llama.cpp)
docker compose --profile gpu run --rm --no-deps all-in-one --provision-only   # tire le GGUF (~6 Go)
docker compose --profile gpu up -d         # db → migrate-gpu → all-in-one
curl -fsS http://localhost:7870/health     # → 200
```

**What runs inside the container, on the GPU** (sequenced by the VRAM autonomy):

- **STT**: `whisper` (no token) or `cohere` (with token, reference quality);
- **Diarization**: `sortformer` (NVIDIA, no token, ≤4 speakers, experimental) or `pyannote`
  (with token, unlimited speakers);
- **Arbitration LLM** (summary / correction / final review): **`llama-server` compiled in the
  image**, serving a small GGUF (`TRANSCRIA_LLM_TIER` tier, default 12 GB = Qwen3.5-9B Q5_K_M,
  **non-gated**) downloaded at runtime into the `models` volume. opencode (the agent) is included;
  its `provider.local` is pointed at `127.0.0.1:8080` at startup.

→ **Without any token**, all 7 profiles work (speakers via Sortformer ≤4). A **free HF
token** (+ the terms of both models) switches to **reference quality** (Cohere +
pyannote, unlimited speakers). No weights are in the image (hermetic build).

> The `all` role launches the built-in Flask server: suitable for **test/demo**, not for
> high-traffic production (prefer then the `web` gunicorn + `scheduler` split, where the
> arbitration LLM stays an external service — see the `TRANSCRIA_ARBITRAGE_LLM_HOST` matrix entry).
>
> **Why CUDA 12.8 + cu130**: the RTX 50xx (Blackwell, sm_120) require torch cu128+ kernels
> and nvcc ≥ 12.8; the `torchcodec>=0.12` pin (pyannote 4.x) is only published on
> cu126 and cu130 → cu130 (driver ≥ 580 required).
> **Why compiled llama.cpp**: llama.cpp does not publish a Linux CUDA binary → we compile it
> in a builder stage (the canonical binary of the tiers).

#### `:bundled` image — embedded models (zero-download, offline)

Variant of the all-in-one GPU that **embeds the non-gated default models** instead of
downloading them at runtime: whisper large-v3 (MIT), Sortformer 4spk (NVIDIA Open Model License), Voxtral Mini 3B (Apache-2.0 — secondary of the targeted multi-STT, **enabled by default** since 0.3.4), MOSS-Transcribe-Diarize (Apache-2.0 — opt-in "one-pass ASR+speakers" backend, weights **and** isolated Transformers 5 site baked into `/opt/transcria-moss-site`, symlinked at startup onto the config default), the
Qwen3.5-9B Q5_K_M arbitration LLM (Apache-2.0) **and** the **SQUIM** audio qualification model
(torchaudio, ~29 MB) — the only model the pipeline still downloaded at runtime (DNSMOS is already
an `.onnx` versioned in the repository). Result: **no download on first run** (validated E2E).
Image `Dockerfile.allinone-bundled`; tag `ghcr.io/<owner>/transcria-allinone:bundled`.

```bash
scripts/docker_quickstart.sh --bundled        # pull :bundled si publiée, sinon build local
```

| | `:latest` (slim) | `:bundled` |
|---|---|---|
| Default models | downloaded on first run | **baked into the image** |
| HF cache | bind of the **host** cache | **named volume** `hfcache` (seeded) |
| First startup | network required (~12 GB) | **offline, instant** |
| `[Errno 17] File exists` trap | possible (host cache) | **removed** |
| Image size | ~19 GB | ~40 GB |
| `/licenses/` (attributions) | n/a (nothing baked) | **included** (NOTICE + NVIDIA OML + MIT) |

This is **not** a "full" image: pyannote/Cohere (gated) and the LLM tiers > 12 GB are
**not** embedded — they remain opt-in (`HF_TOKEN` → cohere+pyannote; `TRANSCRIA_LLM_TIER` →
bigger GGUF, downloaded into the volume). The redistribution licenses of the baked models have
been checked (Qwen Apache-2.0, whisper MIT, Sortformer NVIDIA Open Model License, SQUIM CC-BY-4.0;
the NVIDIA OML requires attaching the agreement + the attribution "Licensed by NVIDIA Corporation under the
NVIDIA Open Model License": this is done in `/licenses/`).

#### GPU / VRAM prerequisites

**Compatible GPUs**: you need **compute capability ≥ 7.5** **AND ≥ 8 GB** of VRAM (on a card
< 12 GB, the 8 tier — Qwen3.5-4B, ~6.4 GB loaded — is picked automatically; from 12 GB
the default is the 9B ~10.6 GB, see the VRAM table below). `llama-server` embeds the SASS
`sm_75→sm_90` **+ `sm_120` (native RTX 50xx since 0.4.4)** + the PTX of the highest tier which
JITs to even newer architectures. **NVIDIA driver ≥ 580 required** (torch cu130 /
CUDA 13 — 0.4.3 images for older drivers). torch cu130 covers STT/diar,
sm_120 kernels included.

| Generation (compute) | Status | Cards **≥ 8 GB** (examples, tier 12 from 12 GB) |
|---|---|---|
| Pascal — GTX 10xx, P40/P100 (6.x) | ❌ not supported | — |
| Volta — V100, TITAN V (7.0) | ❌ not supported (`< 7.5`) | — |
| **Turing** (7.5) | ✅ native | RTX 2060 12G, TITAN RTX 24G, T4 16G, Quadro RTX 6000/8000 |
| **Ampere** (8.0 / 8.6) | ✅ native | RTX 3060 12G, 3080 12G / 3080 Ti, 3090(Ti) 24G, A10/A40/A100, A5000/A6000 |
| **Ada** (8.9) | ✅ native | RTX 4070(Ti/Super) 12-16G, 4080 16G, 4090 24G, L4/L40(S), RTX 5000/6000 Ada |
| **Hopper** (9.0) | ✅ native | H100 80G, H200 141G |
| **Blackwell RTX 50xx** (12.0) | ✅ **native since 0.4.4** ¹ | RTX 5070 12G, 5080 16G, 5090 32G |
| **Blackwell datacenter** (10.x) | ✅ via **PTX JIT** ¹ | B100/B200 |

> ¹ RTX 50xx: `sm_120` SASS embedded (llama.cpp, audio.cpp, parakeet.cpp) + torch cu130 —
> **driver ≥ 580 required**. B100/B200 (sm_100): PTX JIT on first launch (slower once,
> then cached).
> The **8-11 GB consumer cards** (RTX 2070/2080, 3060 Ti, 3070, 4060(Ti), 5060…) are covered since
> 0.4.2: too tight for the 9B, they automatically receive **tier 8** (Qwen3.5-4B,
> ~6.4 GB loaded, baked into the bundled image). Below 8 GB, no LLM fits (transcription only).

**VRAM — NOT additive** (verified on the E2E logs): the VRAM autonomy loads/unloads the models
**sequentially** — each phase reserves then **releases** the GPU before the next one (STT → released →
diarization → released → LLM launched → released). The **peak ≈ the biggest phase**, not the sum.
Real footprints (zero-token path, measured):

| Phase | Actual VRAM | Role |
|---|---|---|
| **9B LLM** (12 GB tier, Q5_K_M) | **~10.6 GB** | sizing link (≥ 12 GB) |
| **4B LLM** (8 GB tier, Q5_K_M) | **~6.4 GB** | sizing link (8-11 GB cards) |
| Whisper large-v3 (fp16) | < 5 GB | STT |
| Sortformer | ~3.5 GB | diarization |
| pyannote (reference) | ~2 GB | diarization (with token) |

→ **A single ~12 GB GPU** (Turing 7.5+) is enough for the complete workflow: the 9B (heaviest
phase) is loaded **after** the STT/diar has been released. The price of non-additivity: reloading
the models between phases is **slower** (no co-residency). **16 GB+ is comfortable**; a higher LLM
tier or reference quality (cohere ~6 GB + pyannote) requires more / multi-GPU.

> The quickstart **automatically aligns** `gpu.llm_vram_mb` on the **effective** tier (12 GB →
> `12000`; card < 12 GB → tier 8 → `8000`, mirroring the entrypoint's downgrade) — otherwise
> the default `60000` (64 GB tier) would make the LLM's **admission be refused** on any real card.

### Publishing a public image (GHCR)

The all-in-one GPU image is **publishable**: permissive licenses (project Apache-2.0, llama.cpp
MIT, NeMo Apache-2.0, opencode MIT, torch BSD, redistributable CUDA base) and **no embedded
weights**. The `.github/workflows/publish-image.yml` workflow builds and pushes
`ghcr.io/<owner>/transcria-allinone:<tag>` (+ `:latest`) on push of a `v*` tag or via
`workflow_dispatch`.

On the tester's side, pointing the quickstart at the published image does a **`pull`** (no build):

```bash
TRANSCRIA_ALLINONE_IMAGE=ghcr.io/<owner>/transcria-allinone:vX.Y.Z scripts/docker_quickstart.sh
```

> **Minimum driver**: torch cu130 (CUDA 13) → NVIDIA driver **≥ 580** (Linux, since 0.4.4;
> use the 0.4.3 images on 535-579 drivers). If the driver is older,
> the quickstart falls back to a **local build**. The image is large (**~19 GB**: CUDA
> devel base + torch + NeMo); the CI build is heavy, the workflow
> frees the runner's disk space; otherwise build/push from a local GPU machine.
>
> *Validated E2E (2026-06-23, 8× RTX 3090): complete pipeline, quality 97/100, SRT/ZIP/DOCX deliverables.*

**Registry build cache (0.3.6)**: the pinned CUDA stages (llama.cpp, audio.cpp,
parakeet.cpp) exceed the GitHub runner's budget when compiling cold (a kill was experienced at
180 min). The workflow reads the `:buildcache`, `:buildcache-runtimes`,
`:buildcache-llama` refs and maintains `:buildcache` on every successful publish. After a
**bump of a pinned SHA**, re-seed the cache from a beefy machine BEFORE pushing the tag:

```bash
docker buildx create --name seedcache --driver docker-container   # une fois
docker buildx build --builder seedcache --target stt-runtimes-builder -f Dockerfile.allinone-gpu \
  --cache-to type=registry,ref=ghcr.io/<owner>/transcria-allinone:buildcache-runtimes,mode=max .
docker buildx build --builder seedcache --target llama-builder -f Dockerfile.allinone-gpu \
  --cache-to type=registry,ref=ghcr.io/<owner>/transcria-allinone:buildcache-llama,mode=max .
```

#### Publishing the `:bundled` image (SCRIPTED ritual — wave C7)

The `:bundled` image (~40 GB with the weights) **exceeds the disk of a standard GitHub runner** → it
is **not** built in CI (the `publish-image.yml` workflow only publishes the slim one). It is
built and pushed **from a GPU machine**, exclusively via the script (which chains:
`test_docker_sync` guards, `:bundled` + `:v<version>-bundled` build, **blocking verification of
the content inside the container** — package version, runtimes `COMMIT` == Python constants,
MOSS site, baked weights, absence of `/app/runtimes` — then GHCR push on explicit request):

```bash
# 1. Construire et vérifier (réseau requis : ~21 Go de poids NON gated, aucun token) :
scripts/release_bundled.sh
# 2. Pousser (login GHCR via `gh auth token`) :
scripts/release_bundled.sh --skip-build --push
# 3. Rendre le package PUBLIC une fois (Settings → Packages → Change visibility).
```

On the tester's side afterwards: `scripts/docker_quickstart.sh --bundled` does a simple **`pull`**.

### MEETING BOT images — separate by design

Two additional images serve to make an automated participant **join a meeting**. They are
**never** in the application image, and that is no oversight: the first embeds Chromium, the
second 207 MB of native Zoom library. Folding them into the default image would make it grow for
every deployment that transcribes no live meeting — that is, the majority.

| File | What it builds | When to use it |
|---|---|---|
| `Dockerfile.bot` | BROWSER bot (headless Chromium, in-page WebRTC capture) | Jitsi, and any service without an SDK |
| `Dockerfile.zoom-sdk` | native ZOOM bot (official Linux Meeting SDK, no browser) | Zoom — **the only path proven in a real meeting** |
| `docker-compose.bot.yml` | launch of the BROWSER bot, one EPHEMERAL container per meeting (`run --rm bot <url>`) — it does not build the Zoom image | Jitsi, live |
| `docker/zoom_sdk_entrypoint.sh` | D-Bus/audio bootstrap required by the Zoom SDK | included in the image |
| `docker/zoom_sdk_verify_libs.py` | BUILD guardrail: is the native environment complete? | included in the image |

⚠ **The two `docker/zoom_sdk_*` scripts are not packaging details.** The Meeting SDK
is not a network library but a full ZOOM CLIENT: without a D-Bus bus or an audio subsystem,
it returns no error — it **crashes with a segfault**, mid-meeting. The build guardrail
fails by naming the missing library rather than letting the problem be discovered
in production. Both files carry the full reasoning in their headers.

**Do not build these images by hand** for everyday use: `scripts/bot.sh` picks
the image by platform, **builds it if missing**, sets the network mode and reads
`~/.transcria-bot.env`. It is the only command to know. Full procedure, credentials and
measured quality: **[docs/BOT_REUNION.md](BOT_REUNION.md)**. The status of each platform and
the steps to enable it: the portal's **`/admin/connecteurs`** page.

### "Legacy" GPU override (old Docker daemons)

`docker-compose.legacy-gpu.yml` replaces **CDI** mode with the old `runtime: nvidia`. Useful
only where the Docker daemon predates version 25 and ignores CDI — Ubuntu/Debian's
`docker.io`, or a nested LXC.

⚠ **This file is NOT versioned** (it is in `.gitignore`): it is machine-dependent, and
a fresh clone will not have it. Here it is in full, to recreate if needed:

```yaml
# Surcharge LOCALE : GPU en mode legacy `runtime: nvidia` pour les démons Docker < 25 sans CDI.
services:
  migrate-gpu:
    devices: []
    runtime: nvidia
    environment:
      NVIDIA_VISIBLE_DEVICES: all
  all-in-one:
    devices: []
    runtime: nvidia
    environment:
      NVIDIA_VISIBLE_DEVICES: all
```

To stack on top of the main compose:

```bash
docker compose -f docker-compose.yml -f docker-compose.legacy-gpu.yml --profile gpu up -d
```

If `docker compose --profile gpu` fails with a device error while
`nvidia-smi` works on the host, this is the first thing to try.

### Separate GPU resource node (split deployment)

```bash
docker run -d --device nvidia.com/gpu=0 -e TRANSCRIA_ROLE=resource-node \
    -v $PWD/config.yaml:/app/config.yaml:ro -v $PWD/.env:/app/.env:ro \
    -v $PWD/models:/app/models -p 8002:8002 transcria:latest
```

`resource-node` does not require the application database; it exposes `/capabilities` (which
enumerates the GPUs seen by the container) and `/engines/ensure`. The scheduler references it via
`inference.mode=remote`.

### Full split GPU bench with vLLM (Cohere STT + arbitration LLM)

For a **fully containerized** split deployment where the GPU node ALSO serves the STT and
the arbitration LLM via **vLLM** (instead of external services), a dedicated bench is provided:
`docker-compose.split-gpu.yml` + `config.split.example.yaml`. Detailed reference (decisions,
risks, VRAM placement, FP8 on Ampere): **[docs/archive/PLAN_TEST_SPLIT_VLLM.md](archive/PLAN_TEST_SPLIT_VLLM.md)** (French)
(the tuning plan, archived — the bench itself remains in service).

A **development override** comes with it: `docker-compose.split-gpu.dev.yml` mounts the
host's source code into the containers to iterate without rebuilding the image.

```bash
docker compose -f docker-compose.split-gpu.yml -f docker-compose.split-gpu.dev.yml up -d
```

⚠️ **Never for a final validation**: that one must run on the baked images, otherwise
you validate the host's code and not what the image contains.

Specifics vs the minimal `docker run` above:

- **Images built via `install.sh`** (we test the install like a user would), **hermetic
  builds — no PostgreSQL database required**: the worker goes through `install.sh --pg-defer`
  (writes the DSN without connecting; the schema is applied at runtime by the `migrate` job).
  ```bash
  docker build -f Dockerfile.worker        -t transcria-worker:latest .
  docker build -f Dockerfile.resource-node -t transcria-resource-node:latest .   # base CUDA + venv vLLM
  ```
  The worker ships opencode (installed by `install.sh`, `scheduler` profile); the node adds an
  **isolated vLLM venv** (`/opt/vllm-venv`) next to the project venv (torch cu130) — the two
  torch stacks do not mix.
- **opencode** is installed at build time, and its `provider.local` is **reconfigured at startup**
  (entrypoint) from the mounted config → it points at the node's arbitration vLLM. The **base
  image** (`Dockerfile`, `scheduler`/all-in-one profiles) now also installs opencode via the
  official installer (it is built by `pip install`, not by `install.sh`): the roles that
  execute the LLM phases have it whatever the topology.
- **Cohere STT** served by vLLM in the node (`/engines/ensure` launches `launch_stt_cohere.sh`,
  `STT_BIN` = vLLM venv); **arbitration LLM** = the `vllm-arbitrage` service (Qwen3.6-27B-FP8, TP=4,
  FP8 Marlin on Ampere) via `scripts/launch_arbitrage_vllm.sh`.
- All **8 GPUs** are exposed (`nvidia.com/gpu=all`): the VRAM autonomy code places arbitration
  (TP=4) + STT + diarization (`device: auto`).

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

## Rollback procedure

- **Code rollback WITHOUT a schema change** (both versions share the same
  Alembic revision): redeploy **only the application services** with `--no-deps`
  so as **not to replay `migrate`** (a normal `up` would relaunch it, since `migrate` is
  outside the profile):
  ```bash
  docker compose --profile split stop web scheduler
  TRANSCRIA_IMAGE=transcria:<tag-précédent> \
    docker compose --profile split up -d --no-deps web scheduler
  # tout-en-un : --profile gpu … --no-deps all-in-one
  ```
- **Rollback across a schema migration**: a `migrate` from the **old** image
  would fail on an unknown revision (and the data may be incompatible).
  Safe procedure: **restore the compatible PostgreSQL backup** (`pg_restore`) taken
  before the upgrade, *then* redeploy the old image. **Keep a `pg_dump`
  before every `migrate`.**
- ⚠️ **The target image must exist.** `TRANSCRIA_IMAGE=transcria:<tag>` only triggers a real
  rollback if that image is present locally (or pulled from a registry). Otherwise, since a
  `build:` is defined, Compose **rebuilds the current code** under that old tag — a fake
  rollback. Keep the images of deployed versions (or publish them to a registry, see the
  0.x backlog), ideally referenced by digest in production.
- **Manifest compatibility**: the rollback reuses the `docker-compose.yml` and the
  `config.yaml` of the **current** checkout with an **old** image. It therefore assumes the
  contracts have not changed between the versions (role names, entrypoint commands,
  environment variables, volume paths, `config.yaml` format). For a fully
  reproducible rollback, version **together**: image + `docker-compose.yml` + `config.yaml`
  + associated Alembic revision (see the 0.x backlog: immutable images on a registry).
- **Job data**: the `jobs`/`models` volumes persist independently of the
  containers; a code rollback does not touch them.

## Volumes

| Volume | Mounted in | Contents |
|---|---|---|
| `pgdata` | `db` | PostgreSQL data |
| `jobs` | `web` + `scheduler` (split), `all-in-one` (gpu) | Job workspaces — **volume shared** between web and scheduler (single host → `shared_backend: fs` is enough) |
| `models` | `web` + `scheduler` (split), `all-in-one` (gpu) | Local models/caches |
| `hfcache` | `all-in-one` (gpu, **`:bundled` mode** only) | HF cache seeded from the image (`TRANSCRIA_HF_SOURCE=hfcache`); replaces the host cache bind |
| `./config.yaml` (bind, ro) | all | Application configuration |
| `./.env` (bind, ro) | all | Secrets (Flask key, API keys…) |

## Validation status

Actually verified (build + run) on Fedora 42, Docker 29, 8× RTX 3090, driver 580:

- ✅ **CPU**: `migrate` (3 Alembic migrations in a container, exit 0), `web` (gunicorn, `/health` 200).
- ✅ **GPU**: CUDA image (torch 2.12+cu130), `torch.cuda` + GPU matmul inside the container;
  `resource-node` role (gunicorn `inference_service`, `/health` 200, `/capabilities` enumerates the RTX 3090);
  **all-in-one `--profile gpu`** (`/health` 200, GPU visible) via CDI.
- ✅ **Real E2E** (`tests/test_e2e_workflow.py --audio tests/test2.mp3 --mode quality --skip-llm`,
  HF_TOKEN + mounted cache): Cohere STT + pyannote diarization **on GPU in a container** →
  **29 segments, 2 speakers, SRT 2630 chars, quality score 97/100**. The pipeline's transcription
  and diarization work fully in a container.

Not covered / external dependencies:

- **LLM correction/summary**: not exercised here (`--skip-llm`) — they require a reachable
  OpenAI-compatible LLM. **Arbitration LLM** = external service (not containerized by this compose).
- TLS reverse proxy (nginx): see `deploy/nginx-transcria.conf.example`.

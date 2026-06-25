# TranscrIA

[![CI](https://github.com/Martossien/transcria/actions/workflows/tests.yml/badge.svg)](https://github.com/Martossien/transcria/actions/workflows/tests.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-ready-336791.svg?logo=postgresql&logoColor=white)](docs/INSTALL.md)

**Self-hosted meeting transcription portal** — turn long meeting recordings into usable deliverables on **your own GPUs**: corrected, speaker-attributed transcripts (SRT), structured summaries, quality reports and meeting-type-aware Word minutes. No cloud, no per-minute API bill, full data sovereignty.

> 🇫🇷 *Interface et documentation actuellement en français — [README français](README.fr.md). The product is French-first today; UI strings and LLM prompts are centralized so localization is a planned evolution, not a rewrite.*

![Job pipeline — guided 9-step workflow with audio diagnosis](docs/screenshots/02-job-pipeline.png)

## Why TranscrIA

Plenty of scripts wrap Whisper. TranscrIA is built as a **service** for teams that process real meetings, week after week:

- **A real audio module, not an `ffmpeg` wrapper.** Acoustic preflight (SNR, clipping, bandwidth, risk flags), speech/music/noise scene analysis, a **per-window difficulty timeline** shown to the user *before* transcription, optional Demucs source separation, loudness normalization, Silero VAD — all coordinated with GPU/VRAM management.
- **Human-in-the-loop where it matters.** Detected speakers come with playable audio excerpts, talk time and an acoustic gender hint; users validate names, participants and a domain lexicon before the final pass. Known-voice matching is consent-based (signed form, hashed proof, source audio deleted by default).
- **LLM arbitration with guardrails.** A local OpenAI-compatible LLM (e.g. llama.cpp) produces the structured summary, corrects the SRT using the validated lexicon and context, and a final review pass harmonizes the deliverables — with anti-hallucination cleanup, retry-then-fail-loud semantics, and **prompts editable in the admin UI**.
- **Production-grade orchestration.** Persistent GPU job queue (priorities, anti-starvation aging, pause/resume, scheduled starts), **VRAM-aware admission** per remaining pipeline phase, calendar-based GPU scheduling, a **resumable pipeline** (checkpoint/resume — a re-queued job never redoes finished work), and "waiting for VRAM" as a first-class, admin-alerted state instead of a silent failure.
- **Three deployment topologies.** All-in-one box; **CPU-only web frontend + GPU worker** (shared PostgreSQL, job files replicated **through the database** — no NFS to operate, sha256-verified integrity); and a remote **inference node** serving STT/diarization/voice-embedding over HTTP with VRAM autonomy (reuse → launch on demand → explicit 503).
- **Compliance by design.** Multi-user RBAC (roles, groups), full **GDPR audit trail** (actor, IP, timestamp, filterable, exportable), consent-gated voice profiles, secrets kept out of the versioned config.

## Screenshots

**Home — jobs at a glance, one-click SRT / ZIP downloads**

![Home — job list](docs/screenshots/01-home.png)

**Processing profiles — pick your deliverable on a single slider right after upload; the portal pre-selects the most complete profile your hardware can run and hides the steps it doesn't need**

![Processing profile selector](docs/screenshots/07-profile.png)

**Speaker validation — listen to excerpts, name speakers, acoustic gender hints**

![Speaker validation step](docs/screenshots/06-speakers.png)

**Configuration — detected hardware, friendly forms, LLM prompts editable in-app, full YAML for experts**

![Configuration editor](docs/screenshots/03-configuration.png)

**GPU scheduling & queue — calendar windows (block night starts, cap concurrency), persistent queue with priorities**

![GPU scheduling calendar](docs/screenshots/04-scheduling.png)

![Persistent GPU queue](docs/screenshots/05-queue.png)

## How it works

```
upload ─► audio diagnosis ─► quick summary (STT + LLM) ─► context, participants,
   lexicon (human validation) ─► final pipeline:
   preprocess → transcription → diarization → LLM correction → final review
   → quality scoring → exports (SRT, segments, quality report, DOCX minutes, ZIP)
```

- **STT backends** (interchangeable): Cohere transcribe (default), Whisper large-v3 / faster-whisper, IBM Granite Speech, NVIDIA Parakeet TDT (experimental) — served locally or by a remote OpenAI-compatible server (vLLM, SGLang…).
- **Diarization backends**: pyannote.audio (default) or NVIDIA Sortformer via NeMo.
- **Word minutes adapted to 18 meeting types** (works council, executive committee, project review, crisis…): LLM-extracted decisions/actions/votes, type-specific fields and visual themes, graceful degradation if extraction fails.
- **Processing profiles** (after upload): pick a *deliverable* on a single slider — from a quick `SRT express` to a full `dossier qualité` — instead of an opaque fast/quality switch. The portal greys out profiles your hardware can't run, pre-selects the most complete one that fits, and then only executes the pipeline phases (and only reserves the GPU/LLM) that the chosen profile actually needs.
- Every phase is **checkpointed**: a re-dispatched job resumes at the first incomplete phase, even on a different worker.

## Quickstart

```bash
git clone https://github.com/Martossien/transcria.git
cd transcria
./install.sh            # venv, dependencies, CUDA-matched PyTorch, config.yaml, optional systemd unit
```

**Arbitration LLM, auto-selected by VRAM.** During install, TranscrIA detects your GPUs and **recommends the largest tier that actually fits** (12 / 16 / 24 / 32 / 48 / 64 GB) — by real per-card placement (mono or split), not by total VRAM — and offers to **download the right GGUF** (with your HF token) and activate it — one prompt, no manual model-picking. Below 12 GB it falls back to **raw transcription** (no correction/summary LLM). The per-tier models are benchmarked in [docs/BENCH_LLM_PALIERS.md](docs/BENCH_LLM_PALIERS.md); switch anytime with `scripts/switch_arbitrage_llm.sh <tier>`.

Still bring your own STT weights and pyannote cache (see [docs/INSTALL.md](docs/INSTALL.md)), fill in `config.yaml`, then validate the install with the built-in preflight — no GPU needed, no side effects:

```bash
venv/bin/python scripts/doctor.py            # config, DB schema, LLM server, opencode, nodes, storage
venv/bin/python scripts/doctor.py --strict   # warnings become failures (for deployment gates)
```

Start the service (`./start.sh` or systemd) and open the web UI. For distributed setups (web frontend + GPU worker, remote inference node), see [docs/INSTALL.md §11–13](docs/INSTALL.md) and [docs/STOCKAGE_PARTAGE_JOBS.md](docs/STOCKAGE_PARTAGE_JOBS.md).

### …or run it with Docker (one command)

Prefer containers? A turnkey script takes you from clone to a running stack — host GPU setup, secret/config generation, image build, `docker compose up`, health check — with no manual steps:

```bash
scripts/docker_quickstart.sh                  # all-in-one GPU → http://localhost:7870 (admin / see config.yaml)
HF_TOKEN=hf_xxx scripts/docker_quickstart.sh  # with the gated Cohere STT; omit it to use whisper (no token)
scripts/docker_quickstart.sh --cpu            # no GPU (web + scheduler)
scripts/docker_quickstart.sh --down           # stop
```

It is idempotent (never overwrites an existing `config.yaml`/`.env`) and validated end-to-end on GPU (real in-container transcription). Full reference — image, compose, GPU enablement, variables, rollback — in [docs/DOCKER.md](docs/DOCKER.md).

## Tech stack

| Layer | Technology |
|---|---|
| Backend | Python 3.11+, Flask 3, SQLAlchemy + Alembic (PostgreSQL in production, SQLite for dev) |
| STT serving | vLLM / SGLang / any OpenAI-compatible server; local engines |
| Diarization & voice | pyannote.audio, NVIDIA NeMo (Sortformer), local voice embeddings |
| LLM phases | [opencode](https://github.com/sst/opencode) driving a local OpenAI-compatible LLM (llama.cpp…) |
| Audio | ffmpeg/ffprobe, Demucs, Silero VAD, SQUIM / DNSMOS quality metrics |
| Frontend | Server-rendered Jinja2 + Bootstrap 5, vanilla JS |
| Exports | python-docx (themed minutes), SRT, JSON, ZIP package |

## Project status

⚠️ **Beta — latest release: [`v0.1.0-beta.5`](https://github.com/Martossien/transcria/releases/tag/v0.1.0-beta.5).** The product is functional and covered by **2,668 tests (green CI: ruff, mypy, full pytest on PostgreSQL, ~80 % coverage)**. The installer is validated end-to-end on **4 Linux distributions (Ubuntu 22.04/24.04, Debian 12, Fedora 42) × Python 3.11–3.13** (apt + dnf, systemd and non-systemd, PostgreSQL 14/15/16), full pipeline STT + diarization + LLM. The **distributed topology** (CPU frontend + GPU resource node) is validated **end-to-end on real audio**: STT (Cohere) + diarization served remotely and a vLLM arbitration LLM (Qwen3.6-27B-FP8, tensor-parallel), with automatic VRAM placement across 8 GPUs — see [docs/DOCKER.md](docs/DOCKER.md) and [docs/PLAN_TEST_SPLIT_VLLM.md](docs/PLAN_TEST_SPLIT_VLLM.md). **Concurrency hardened under load**: the split topology is robust up to 8 concurrent jobs (graceful degradation, no crash), throughput scaling to a hardware sweet spot, with the vLLM engines batching concurrent requests — see [docs/PLAN_TEST_CHARGE.md](docs/PLAN_TEST_CHARGE.md). Following SemVer, the **`0.x` series is a stabilization phase**: the API, the configuration schema and the data model may still change without backward-compatibility guarantees until `1.0.0`. Evaluate it, pilot it — don't bet production on it without your own validation. A containerized deployment (Dockerfile, compose, GPU support, turnkey quickstart) is available — see [docs/DOCKER.md](docs/DOCKER.md).

**Language**: the UI and the LLM prompts are French-first (the pipeline is tuned for French meetings). Both are centralized/editable, so adding languages is a planned evolution, not a rewrite.

## Documentation

Full documentation lives in [`docs/`](docs/) (currently in French):

| Document | Content |
|---|---|
| [docs/INSTALL.md](docs/INSTALL.md) | Installation, models, systemd, troubleshooting, **distributed deployment** |
| [docs/DOCKER.md](docs/DOCKER.md) | **Containerized deployment** — turnkey quickstart, image, compose, GPU (CDI), variables, rollback |
| [docs/TECHNICAL.md](docs/TECHNICAL.md) | Architecture, pipeline, API, GPU orchestration |
| [docs/CONFIG_REFERENCE.md](docs/CONFIG_REFERENCE.md) | Complete `config.yaml` reference |
| [docs/DATA_MODEL.md](docs/DATA_MODEL.md) | DB schema, job states, files per job |
| [docs/SERVICE_RESSOURCES_GPU.md](docs/SERVICE_RESSOURCES_GPU.md) | Remote inference, VRAM autonomy, degraded modes |
| [docs/STOCKAGE_PARTAGE_JOBS.md](docs/STOCKAGE_PARTAGE_JOBS.md) | PostgreSQL-backed job file store for split deployments |
| [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) · [CHANGELOG.md](CHANGELOG.md) | Contributing, security policy, changelog |

## License

TranscrIA is released under the [Apache License 2.0](LICENSE). Third-party components
(bundled libraries and binaries, and runtime-downloaded models) and their licenses /
attributions are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) — including the
CC-BY-4.0 attribution for the DNSMOS/SQUIM quality models, and the licenses of components
shipped in the Docker images (opencode — MIT, ffmpeg — GPL/LGPL via Debian, etc.). No
GPL/AGPL (strong copyleft) dependency is present at runtime.

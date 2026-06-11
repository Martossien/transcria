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
- Every phase is **checkpointed**: a re-dispatched job resumes at the first incomplete phase, even on a different worker.

## Quickstart

```bash
git clone https://github.com/Martossien/transcria.git
cd transcria
./install.sh            # venv, dependencies, CUDA-matched PyTorch, config.yaml, optional systemd unit
```

Bring your own models (STT weights, pyannote, a GGUF for the arbitration LLM — see [docs/INSTALL.md](docs/INSTALL.md)), fill in `config.yaml`, then validate the install with the built-in preflight — no GPU needed, no side effects:

```bash
venv/bin/python scripts/doctor.py            # config, DB schema, LLM server, opencode, nodes, storage
venv/bin/python scripts/doctor.py --strict   # warnings become failures (for deployment gates)
```

Start the service (`./start.sh` or systemd) and open the web UI. For distributed setups (web frontend + GPU worker, remote inference node), see [docs/INSTALL.md §11–13](docs/INSTALL.md) and [docs/STOCKAGE_PARTAGE_JOBS.md](docs/STOCKAGE_PARTAGE_JOBS.md).

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

⚠️ **Active development — no tagged release yet.** The product is functional and covered by **1,800+ tests (green CI: ruff, mypy, full pytest on PostgreSQL)**, but the API, the configuration schema and the data model may still change without backward-compatibility guarantees. Evaluate it, pilot it — don't bet production on it without your own validation. Docker images are planned once things stabilize.

**Language**: the UI and the LLM prompts are French-first (the pipeline is tuned for French meetings). Both are centralized/editable, so adding languages is a planned evolution, not a rewrite.

## Documentation

Full documentation lives in [`docs/`](docs/) (currently in French):

| Document | Content |
|---|---|
| [docs/INSTALL.md](docs/INSTALL.md) | Installation, models, systemd, troubleshooting, **distributed deployment** |
| [docs/TECHNICAL.md](docs/TECHNICAL.md) | Architecture, pipeline, API, GPU orchestration |
| [docs/CONFIG_REFERENCE.md](docs/CONFIG_REFERENCE.md) | Complete `config.yaml` reference |
| [docs/DATA_MODEL.md](docs/DATA_MODEL.md) | DB schema, job states, files per job |
| [docs/SERVICE_RESSOURCES_GPU.md](docs/SERVICE_RESSOURCES_GPU.md) | Remote inference, VRAM autonomy, degraded modes |
| [docs/STOCKAGE_PARTAGE_JOBS.md](docs/STOCKAGE_PARTAGE_JOBS.md) | PostgreSQL-backed job file store for split deployments |
| [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) · [CHANGELOG.md](CHANGELOG.md) | Contributing, security policy, changelog |

## License

TranscrIA is released under the [Apache License 2.0](LICENSE).

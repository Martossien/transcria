# Testing TranscrIA — what to expect, what to report

TranscrIA is developed and validated end-to-end on one hardware family (multi-RTX-3090
Linux boxes). What we need most right now is **runs on hardware and topologies we don't
own**. This page tells you what you are signing up for before you burn disk and bandwidth,
and what a genuinely useful test report looks like. French or English are both welcome.

## Before you start — what to expect

| | |
|---|---|
| **Disk space** | Docker `:latest` (slim): **~19 GB** image + **~12 GB** of model weights downloaded on first start. Docker `:bundled`: **~31 GB** image, weights baked in (works offline). Native install: plan **100 GB SSD** (the jobs directory grows with your audio). |
| **First startup** | `:bundled` starts without any download. `:latest` and native installs download ~12 GB of weights on first run — duration is network-bound. **Please time your first start**: it is one of the data points we are collecting (see the report template). |
| **Models used (default, no token)** | [whisper large-v3](https://huggingface.co/openai/whisper-large-v3) (STT, < 5 GB VRAM) · NVIDIA Sortformer (diarization, ~3.5 GB) · Qwen3.5-9B Q5_K_M (arbitration LLM, ~10.6 GB — the 12 GB tier; larger tiers in [LLM_TIERS.md](LLM_TIERS.md)). With `HF_TOKEN`: Cohere STT + pyannote (reference quality, gated models). |
| **GPU floor** | Compute capability **≥ 7.5** and **≥ 12 GB VRAM** (the quickstart checks this and fails early with a clear message). |
| **Expected result** | A short recording run through the wizard ends with a downloadable package: speaker-attributed `transcription.srt` (raw + corrected), segments JSON with timestamps, a structured summary, Word minutes, and quality reports depending on the chosen profile. |

## The 15-minute smoke test

```bash
git clone https://github.com/Martossien/transcria && cd transcria
scripts/docker_quickstart.sh              # or --bundled (offline) / --cpu (no local inference)
```

1. Open `http://localhost:7870`, log in with `admin` / `CHANGE-ME` — **change the password**.
2. Create a job, upload a short recording (2–5 minutes is plenty), pick a profile.
3. Walk the wizard (speaker validation, lexicon, summary review), download the package.

**Success** = the ZIP opens and contains the SRT, the segments JSON, the summary and the
Word minutes. Anything else — including "it worked but felt confusing" — is exactly what
we want to hear about.

## Configurations we most need tested

- **Multi-GPU** — several cards, especially mixed VRAM sizes: does the LLM tier selection
  and per-card placement do the right thing? (see [LLM_TIERS.md](LLM_TIERS.md) and
  [INSTALL.md](INSTALL.md) § VRAM tiers)
- **CPU frontend + GPU worker** — `docker compose --profile split` or native
  `role=web` / `role=scheduler` on separate machines sharing PostgreSQL
  ([DOCKER.md](DOCKER.md), [INSTALL.md](INSTALL.md) § distributed roles).
- **Remote inference node** — a separate GPU box running `inference_service`, with the
  frontend delegating STT/diarization to it ([SERVICE_RESSOURCES_GPU.md](SERVICE_RESSOURCES_GPU.md)).
- **External PostgreSQL** — pointing TranscrIA at an existing PostgreSQL instance instead
  of the compose-managed `db` container.

## Collecting diagnostics

```bash
# Native install:
venv/bin/python scripts/doctor.py

# Docker:
docker compose exec all-in-one venv/bin/python scripts/doctor.py
```

`doctor` checks GPU/driver, models, database, disk and configuration — paste its output
in every report, it answers half our questions up front.

**Where the logs live:**

| Deployment | Logs |
|---|---|
| Native (systemd) | `sudo journalctl -u transcria -f` — plus `$LOG_FILE` if set (e.g. `/var/log/transcrIA.log`) |
| Docker all-in-one | `docker compose logs -f all-in-one` |
| Docker split | `docker compose logs -f web` / `scheduler` / `migrate` |
| Container states | `docker compose ps` |

**Redact before posting**: job titles, participant names, transcript excerpts, IPs.
Nothing in the template below requires meeting content.

## Report template

Copy this into a [GitHub issue](https://github.com/Martossien/transcria/issues) (bug) or a
[Discussion](https://github.com/Martossien/transcria/discussions) (it worked / questions / impressions):

```markdown
### Environment
- Distribution & kernel: <e.g. Ubuntu 24.04, 6.8.0-xx>
- Docker & NVIDIA Container Toolkit: <docker --version / nvidia-ctk --version> (native install: n/a)
- GPU(s), VRAM & driver: <first lines of nvidia-smi>
- TranscrIA version: <git tag or `transcria.__version__`, e.g. 0.3.3>
- Images & tags: <docker images | grep transcria> (native: n/a)
- Topology: <all-in-one / bundled / split web+scheduler / remote inference node / external PostgreSQL>

### Results
- `doctor` output: <paste>
- Container states (`docker compose ps`): <paste> (native: `systemctl status transcria`)
- Startup time: <compose up → /health green, or service start → UI reachable>
- Demo processing: <success / failed at step X>
- Logs around the failure (redacted): <paste>
```

Partial reports are fine — an environment block plus "stuck at step 3" already helps.

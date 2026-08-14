# From zero to your first meeting minutes

*([Version française](QUICKSTART.md))*

> **On Windows 11?** The WSL2 + Docker path is described step by step in
> [QUICKSTART_WINDOWS.en.md](QUICKSTART_WINDOWS.en.md).

One page, two paths — pick **one**. Common prerequisite: a Linux machine with an NVIDIA
GPU (compute capability ≥ 7.5; **from 8 GB VRAM** — native and Docker alike, slim and
bundled; on a card under 12 GB the 8 GB-tier LLM is picked automatically — and even
**without a GPU**: CPU transcription via the Kroko engine, no
LLM phases) and its driver installed (`nvidia-smi` must respond).

## Path 1 — Docker (try the project, recommended)

**Step 0 — prepare the host (once).** Docker **≥ 25** (Docker CE — the `docker.io`
package from Ubuntu/Debian repos is not enough), then clone the repo and grant GPU
access:

```bash
git clone https://github.com/Martossien/transcria.git && cd transcria
scripts/setup_docker_gpu.sh          # nvidia-container-toolkit + CDI spec + verification
```

**Step 1 — one command.**

```bash
scripts/docker_quickstart.sh --bundled     # → http://localhost:7870
```

`--bundled` pulls the image with models **baked in** (~60 GB — the size of a big video
game — but then: zero downloads, works offline). Without `--bundled`, the slim image is
light but downloads models at the first job.

**Step 2 — first login.** Open `http://localhost:7870`. The quickstart **prints the
credentials in its final message** (`admin` + a generated password, written to
`config.yaml` under `auth.first_admin_password`). Change it on first login — a banner
will insist. Missed the message? Re-run the quickstart (idempotent): it prints the
credentials again.

**Step 3 — first minutes.** "New processing" → drop the meeting audio (or record from
the mic) → pick a **profile** (e.g. *Corrected Word report*) → let it run → download
the DOCX (plus the SRT and the full ZIP).

## Path 2 — Native install (deploy on a GPU host)

```bash
git clone https://github.com/Martossien/transcria.git && cd transcria
./install.sh          # EXPRESS mode: detections, one summary, one confirmation
./start.sh            # migrations, then the server → http://localhost:7870
```

Express mode detects everything (GPU → LLM tier, psql → PostgreSQL, HF token → models)
and shows "here is what I am going to do" before acting; `./install.sh --expert` brings
back the step-by-step questions. Without an HF token, the install picks **whisper +
Sortformer** (no account required) — reference quality (Cohere + pyannote) can be
enabled later from **Administration → Models** with a token.

After login, the **first-run checklist** on the home page points out what is missing
(absent models, GPU not seen…) with a link to fix each item, and disappears once
everything is green. For production: `sudo systemctl enable --now transcria`, and
`venv/bin/python scripts/doctor.py` validates the install at any time.

## Next

- [INSTALL.md](INSTALL.md) — full reference (options, distributed roles, troubleshooting)
- [DOCKER.md](DOCKER.md) — slim/bundled images, GPU, compose, rollback
- [TESTERS.md](TESTERS.md) — the 15-minute smoke test

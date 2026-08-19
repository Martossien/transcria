# TranscrIA installation guide (English, condensed)

> 🇫🇷 The exhaustive reference is the French [INSTALL.md](INSTALL.md) (~2,200 lines —
> every option, every failure mode). This guide covers the paths people actually take,
> in English. If the two ever disagree, the French version is right.
> Deploying with **Docker** instead? See [DOCKER.en.md](DOCKER.en.md).

## 1. Requirements

| Component | Minimum | Recommended |
|---|---|---|
| CPU | 8 cores | 16+ cores |
| RAM | 32 GB | 64 GB |
| GPU | 1× NVIDIA **8 GB** VRAM (compute ≥ 7.5, i.e. RTX 20xx or newer) | 1-2× NVIDIA 24+ GB (RTX 3090/4090/5090) |
| Disk | 100 GB SSD | 500+ GB NVMe |

Since 0.4.2 the **full workflow** (STT + diarization + LLM summary/correction) runs from
**8 GB of VRAM** on a single GPU: models are loaded/unloaded sequentially and the LLM
tier is picked to fit the card (8/12/16/24/32/48/64 GB tiers). More VRAM buys bigger LLM
tiers and fewer reloads. Without a compatible GPU, a CPU-only profile (Kroko) still
transcribes. RTX 50xx (Blackwell) cards need **NVIDIA driver ≥ 580**.

Software: Ubuntu/Debian 22.04+ (or compatible), Python 3.11+ with the `venv` module
(`apt install python3-venv` — it is a separate package on Debian/Ubuntu), NVIDIA driver
535+ (580+ for RTX 50xx), `ffmpeg` (the installer offers to install it if missing),
PostgreSQL 13+ for anything beyond local development (SQLite remains a dev fallback).

```bash
nvidia-smi        # must list your GPU(s) with a CUDA 12.x/13.x driver
```

## 2. The one command (express install)

```bash
git clone https://github.com/Martossien/transcria.git
cd transcria
./install.sh
```

On an interactive terminal, the default **express mode** detects everything — hardware →
LLM tier by real placement, `psql` + sudo + a server answering `pg_isready` → local
PostgreSQL with a generated password, HF token → model choice — then shows **one "here is
what I am going to do" summary**, asks **one confirmation**, and runs the whole thing.
Missing `ffmpeg`? It offers to install it — one consented question, on Debian/Ubuntu
(`ffmpeg`) as well as Fedora/RHEL (`ffmpeg-free`, from the base repositories).
Answering `n` at the summary exits cleanly **before any mutation**.

Three express-specific decisions:

- **If PostgreSQL is missing, the installer offers it** — before the summary, so that the
  summary tells the truth about the database you will actually get. Two situations, two
  questions: *no `psql`* → offer to install the server (`apt-get install postgresql`, or
  `dnf install postgresql-server postgresql` + `postgresql-setup --initdb`, based on the
  family declared in `/etc/os-release`); *server installed but silent* → offer to start
  it only. Nothing is installed without an explicit yes; declining, missing privileges
  (neither root nor sudo) or non-interactive mode all keep SQLite. The verdict then comes
  from **re-probing** (`psql` + `pg_isready`), never from the package manager's exit code.

- **Without an HF token on a fresh config**, the backends switch to the non-gated duo
  picked for your hardware: **whisper + Sortformer** with a ≥ 12 GB GPU; **Kroko (CPU
  STT) + Sortformer** below 12 GB (the whole GPU stays with the tier-8 LLM); Kroko +
  Sortformer both on CPU without a GPU. The reference-quality **Cohere + pyannote**
  stack stays one click away in *Administration → Models* once you have a token.
- **No admin password is asked during install**: on the portal's **first visit**, a
  page asks you to create the administrator account (username + password of your
  choice) — the final summary reminds you.

`./install.sh --expert` restores the historical step-by-step (one question per choice);
`--non-interactive` is the promptless CI mode.

What install.sh does, in order: prerequisites check → venv → PyTorch wheel matching your
CUDA driver (`cu121`/`cu124`/`cu126`, `cu130` from a CUDA 13 driver — required for RTX
50xx) → `requirements.txt` → `config.yaml` generation (auto-detection) → AI model
detection table → **arbitration LLM setup** (backend choice, VRAM tier, GGUF download) →
opencode provisioning → systemd service → `doctor.py` validation → final summary with a
**first-login section**.

What it does **not** do: install NVIDIA drivers or the CUDA toolkit, compile llama.cpp
(a pinned prebuilt binary is offered, or an existing `llama-server` is detected and
qualified), or download gated weights without your token.

### LLM backend (arbitration model)

- **Ollama** — the easy path: self-contained runtime, no compilation, no HF token.
- **llama.cpp** — the control path (fine quantizations, KV `q8_0`, tensor-split);
  recommended for the 12/16/24 GB tiers. CUDA binary resolution: existing
  `llama-server` → pinned ai-dock prebuilt (sha256-verified) → compile if `nvcc` is
  present → clean failure.
- **vLLM** — for the split topology (front-end + GPU resource node), FP8,
  tensor-parallel.

The VRAM tier → model mapping lives in a single data catalog
(`transcria/data/llm_profiles.yaml`); nothing is hardcoded.

## 3. Install modes

| Mode | Command | What you get |
|---|---|---|
| **All-in-one** (default) | `./install.sh` | portal + scheduler + GPU inference + LLM on one machine |
| **GPU resource node** | `./install.sh --profile resource-node` | inference service only (STT/diarization/voice), driven by a remote front-end |
| **Web + scheduler split** | `./install.sh --profile web` / `--profile scheduler` | multi-worker front-end and a single scheduler, PostgreSQL required |

The distributed topology (front-end + GPU nodes, admission control, remote engines) is
documented in [INSTALL.md](INSTALL.md) § 13 (French) and, for containers,
[DOCKER.en.md](DOCKER.en.md).

## 4. First start

```bash
sudo systemctl start transcria        # if the systemd service was installed
# or, without systemd:
./start.sh --port 7870                # picks up the ./venv next to it
```

Open `http://<machine>:7870` — on the **first visit** (empty database) the portal asks
you to **create the administrator account**, and signs you in. Nothing to fish out of a
log file. For automation, set `auth.first_admin_password` in `config.yaml` before first
boot and the account is created silently. Lost password later:
`venv/bin/python -m transcria.maintenance.cli reset-admin-password admin`.

A **first-run checklist** on the home page shows anything still missing (models, GPU,
database), each item with a link to fix it.

## 5. Models

Everything can be driven from *Administration → Models* in the UI (download LLM/STT/
diarization from a config-driven catalog, with disk-space checks and progress).

- **Non-gated default** (no token): whisper (STT) + Sortformer (diarization, NVIDIA,
  ≤ 4 speakers) + a Qwen GGUF for the LLM tier — this is what express installs.
- **Reference quality** (recommended): Cohere ASR + pyannote diarization (unlimited
  speakers). Both are **gated**: set `HF_TOKEN` and accept both models' terms on
  huggingface.co, then switch backends from the Models page.
- Hugging Face downloads use `hf_transfer` (multi-stream) with automatic fallback;
  `TRANSCRIA_NO_HF_TRANSFER=1` forces the classic path behind finicky proxies.

## 6. Verify

```bash
venv/bin/python scripts/doctor.py     # profile-aware preflight: config, DB schema,
                                      # LLM launcher, models, GPU — seconds, no side effects
```

`doctor.py` is the first thing to run whenever a job fails without a clear message. A
full real-GPU end-to-end test exists too: `venv/bin/python tests/test_e2e_workflow.py`.

## 7. Troubleshooting (the short list)

| Symptom | Usual cause / fix |
|---|---|
| Anything odd | run `scripts/doctor.py` first — it names the broken piece |
| `ModuleNotFoundError: torch` | you are outside the venv: `source venv/bin/activate` |
| `CUDA out of memory` | LLM tier too big for the card — lower it in *Administration → Models*; VRAM autonomy sequences phases, it does not stack them |
| Summaries "unavailable" | the arbitration LLM is not up — check `llama-server` (port 8080) or Ollama, and `services.arbitrage_script` |
| `pyannote not available` | `HF_TOKEN` missing or model terms not accepted on huggingface.co |
| Port 7870 busy | another instance (or the systemd service) is running: `sudo systemctl stop transcria` |
| DB error after `git pull` | run the migrations: `venv/bin/alembic upgrade head` |
| Logs | `journalctl -u transcria.service` (systemd) or `/var/log/transcrIA.log` / `<install>/logs/transcrIA.log` |

The French [INSTALL.md](INSTALL.md) § 12 covers ~15 more failure modes in detail.

## 8. Upgrading

In-app: *Administration → Maintenance* checks for a new **verified tag** and can update
+ restart the service (opt-in). By hand:

```bash
git pull && ./install.sh          # idempotent — never rewrites an existing config.yaml
venv/bin/alembic upgrade head
sudo systemctl restart transcria
```

Breaking changes and per-version notes live in [UPGRADE.md](UPGRADE.md) (French).

## 9. Manual install (no install.sh)

For people who want to see every step:

```bash
git clone https://github.com/Martossien/transcria.git
cd transcria
python3 -m venv venv && source venv/bin/activate
pip install --upgrade pip

# PyTorch — match the wheel to your driver (nvidia-smi | grep "CUDA Version"):
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu126
# CUDA 13 driver / RTX 50xx: use --index-url https://download.pytorch.org/whl/cu130

pip install -r requirements.txt
pip install accelerate

python scripts/bootstrap_config.py --output config.yaml   # then review config.yaml

python -m pytest tests/ -q          # most tests run without a GPU
python app.py                       # dev mode — http://localhost:7870
```

Model downloads, systemd unit installation, SSO (OIDC/LDAP/proxy) and personal API
tokens are covered in the French [INSTALL.md](INSTALL.md) §§ 6, 11, and the final
sections.

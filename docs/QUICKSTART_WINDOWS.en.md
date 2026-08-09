# TranscrIA on Windows 11 — gaming PC, WSL2 + Docker

*([Version française](QUICKSTART_WINDOWS.md))*

> **Status: guide verified against the official documentation (Microsoft, NVIDIA,
> Docker, August 2026); real-machine validation in progress.** Feedback is welcome in
> the [Discussions](https://github.com/Martossien/transcria/discussions).

TranscrIA is a Linux application — but Windows 11 runs Linux with full GPU access
through **WSL2**, and our Docker images run on it **unchanged** (containers reach
90-100% of native inference performance: the bottleneck is the card, not the
virtualization). This guide goes from a bare Windows 11 to the first minutes document.

**Requirements:**

| What | How much |
|---|---|
| Windows | Windows 11 (or Windows 10 build 19041+), administrator rights |
| NVIDIA card | GTX 10xx or newer; **from 8 GB of VRAM** for the full workflow |
| Free disk space | **~130 GB** for the bundled image (recommended); ~60 GB for slim — on the drive of **your choice** (C:, D:, E:…), the guided install asks |
| Connection | stable (the recommended image weighs ~60 GB — like a big video game) |

**Which image?** On Windows we recommend the **bundled** one: models baked in, zero
configuration, works offline afterwards — the fewest moving parts for a first install.
Pick the **slim** (~22 GB) if disk or bandwidth is tight: models are then downloaded
from the portal's **Administration → Models** page.

## Recommended path — the guided install (one script does everything)

Open **PowerShell as administrator** (Start menu → type "PowerShell" → right-click →
*Run as administrator*), then paste these two commands:

```powershell
irm https://raw.githubusercontent.com/Martossien/transcria/main/scripts/windows/Install-TranscrIA.ps1 -OutFile "$env:USERPROFILE\Downloads\Install-TranscrIA.ps1"
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\Downloads\Install-TranscrIA.ps1"
```

The script checks your machine (Windows, NVIDIA card, RAM, free space), then asks
**two questions**: *which drive to install on* (C:, D:, E:… — it suggests the one with
the most free space, and everything goes there: Ubuntu AND the Docker data) and
*which image* (bundled recommended / slim). Then it does everything: WSL2 + Ubuntu on
the chosen drive, memory tuning, Docker Desktop, GPU test, downloading and starting
TranscrIA — and opens the browser on the portal at the end.

**Key principle: the script is re-runnable.** If Windows asks for a reboot (or Docker
shows a window on first launch), do what is asked then **run the same command again**
— the script detects what is already done and resumes where it left off. Nothing is
lost, including an interrupted download. (Script messages are currently in French —
plain and short; an English pass is planned.)

> Status: the script follows exactly the manual steps below (verified against the
> official docs); real-machine validation is in progress. If it gets stuck, the
> manual path always works — and feedback in the
> [Discussions](https://github.com/Martossien/transcria/discussions) helps.

## Manual path — step by step

### Step 1 — WSL2 (PowerShell as administrator)

```powershell
wsl --install
```

Reboot when Windows asks, let Ubuntu set itself up (Linux username + password), then:

```powershell
wsl --update
wsl -l -v        # Ubuntu must show VERSION 2
```

If the install hangs at 0%: `wsl --install --web-download -d Ubuntu`.

### Step 2 — The NVIDIA driver (on Windows, and ONLY on Windows)

Install or update the regular GeForce/RTX driver from
[nvidia.com](https://www.nvidia.com/drivers) (any 2022+ driver works, R495 minimum).

> **Golden rule: NEVER install an NVIDIA driver or the `cuda`/`cuda-drivers` packages
> INSIDE Ubuntu/WSL.** The Windows driver is projected into WSL automatically
> (`/usr/lib/wsl/lib/`); a Linux driver would overwrite it and break everything. This
> is the #1 failure seen on forums — NVIDIA's documentation explicitly forbids it.

### Step 3 — Docker Desktop

Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) (WSL2
backend is the default — change nothing). GPU support is **built in**: nothing else to
install. License: free for personal use (and organisations < 250 employees and
< $10M revenue).

### Step 4 — Check that containers see the card

In an Ubuntu terminal (Start menu → Ubuntu):

```bash
docker run --rm --gpus all nvidia/cuda:13.3.1-base-ubuntu24.04 nvidia-smi
```

You should see the `nvidia-smi` table with your card. If "NVIDIA-SMI has failed":
re-read step 2 (a Linux driver was probably installed inside WSL).

### Step 5 — Give WSL enough RAM (32 GB machines especially)

By default WSL takes 50% of the RAM — too tight on 32 GB for a full run. Create
`C:\Users\<you>\.wslconfig`:

```ini
[wsl2]
memory=24GB
processors=8
swap=8GB
[experimental]
autoMemoryReclaim=gradual
sparseVhd=true
```

(On 64 GB, the 32 GB default is fine.) Then `wsl --shutdown` in PowerShell and reopen
Ubuntu.

### Step 6 — TranscrIA

Still in Ubuntu — and **inside the Linux home** (`~`), never under `/mnt/c` (the
Windows filesystem seen from Linux is ~5-7× slower):

```bash
git clone https://github.com/Martossien/transcria.git && cd transcria
scripts/docker_quickstart.sh --bundled     # → http://localhost:7870
```

> With Docker Desktop, **skip `scripts/setup_docker_gpu.sh`** (it installs the NVIDIA
> Container Toolkit, already integrated in the backend). It is only needed for the
> advanced docker-ce path described at the end of this page.

During the ~60 GB download: use Ethernet if you can and **disable sleep** (Settings →
System → Power) — a pull interrupted by sleep starts over from scratch, whereas simply
re-running the command reuses the layers already completed.

### Step 7 — First minutes document

Open `http://localhost:7870` **in your Windows browser** (the port crosses WSL2 by
itself). Log in with `admin` / `CHANGE-ME` (change it, the banner will insist), then:
"New job" → drop the audio → pick a profile → download the DOCX. The detailed journey
is in the [QUICKSTART](QUICKSTART.en.md).

## Troubleshooting — the 5 classic failures

1. **`nvidia-smi` broken inside the container** → an NVIDIA driver/package was
   installed inside WSL. Purge it (`sudo apt purge '*nvidia*' '*cuda*'`) or reset the
   distro (`wsl --unregister Ubuntu`, then step 1).
2. **`C:` fills up and never shrinks** → the WSL virtual disk grows but never shrinks
   on its own, even after `docker rmi`. Clean up from Docker Desktop (Settings →
   Resources → Disk usage), and compact: `wsl --shutdown` then
   `Optimize-VHD -Path <path to ext4.vhdx> -Mode Full` (admin PowerShell). The
   `sparseVhd=true` from step 5 automates this. The Docker disk image can also be
   moved to another drive: Docker Desktop → Settings → Resources → Disk image
   location.
3. **Job killed mid-processing (OOM)** → WSL RAM too low, re-read step 5.
4. **After sleep: downloads/TLS fail, wrong timestamps** → known WSL2 clock drift.
   `wsl --shutdown` and relaunch; disable sleep during a long job.
5. **On a corporate VPN: no network inside WSL** → on recent Windows 11,
   `dnsTunneling` (on by default) fixes most cases; otherwise add `dnsTunneling=true`
   under `[wsl2]` in `.wslconfig`. Some antivirus products slow WSL down badly: an
   exclusion on `%LOCALAPPDATA%\Docker` helps.

**Reaching the portal from another machine at home**: WSL2's NAT does not expose it by
itself. Either a port forward
`netsh interface portproxy add v4tov4 listenport=7870 connectport=7870 connectaddress=localhost`
(admin PowerShell, + firewall rule), or `networkingMode=mirrored` (Windows 11 22H2+)
in `.wslconfig`.

## Appendix — the advanced path without Docker Desktop

To avoid Docker Desktop (enterprise licensing, or preference): install **docker-ce**
inside Ubuntu/WSL (official Docker repository — systemd is active by default in WSL2's
Ubuntu) then run the repo's `scripts/setup_docker_gpu.sh`, which installs the NVIDIA
Container Toolkit and verifies GPU access. Continue from step 6.

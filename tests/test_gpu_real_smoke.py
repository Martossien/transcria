"""Smoke GPU RÉEL (marqueur ``gpu_real`` — vague C4, plan §3.14).

Formalise la frontière fakes ↔ matériel : les ~166 tests GPU/VRAM de la suite
tournent tous à fakes (c'est ce qui permet à la CI sans GPU de couvrir le
domaine) ; CETTE suite, elle, touche la vraie carte et les vrais processus —
exécutable UNIQUEMENT sur la machine de dev GPU (``TRANSCRIA_GPU_REAL=1``),
jamais en CI. Trois fumées, celles faites à la main jusqu'ici :

1. le snapshot de l'allocateur recoupe ``nvidia-smi`` réel ;
2. ``VRAMManager._kill_port`` tue un vrai processus factice qui écoute ;
3. le superviseur STT (planner VRAM réel + lanceur de script réel) lance un
   stub réel, le voit prêt, puis l'arrête via ``scripts/stop_stt.sh``.

C'est le filet outillé de B3 (refactor GPU) : avant/après, cette suite doit
rester verte sur la machine.
"""
import os
import socket
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.gpu_real

if os.environ.get("TRANSCRIA_GPU_REAL") != "1":
    pytest.skip(
        "Smoke GPU réel non demandé (positionner TRANSCRIA_GPU_REAL=1 sur la machine GPU)",
        allow_module_level=True,
    )

from builders import make_config  # noqa: E402


def _nvidia_smi_gpus() -> list[dict]:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=index,memory.total,memory.free", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=10, check=True,
    ).stdout
    gpus = []
    for line in out.strip().splitlines():
        idx, total, free = (part.strip() for part in line.split(","))
        gpus.append({"index": int(idx), "total_mb": int(total), "free_mb": int(free)})
    return gpus


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_listening(port: int, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise AssertionError(f"le processus factice n'écoute pas sur {port} après {timeout_s}s")


def test_allocator_snapshot_matches_real_nvidia_smi(tmp_path):
    """L'inventaire de l'allocateur (torch.cuda.mem_get_info) recoupe nvidia-smi."""
    from transcria.queue.allocator import GPUAllocator

    real = _nvidia_smi_gpus()
    assert real, "nvidia-smi ne rapporte aucun GPU"

    alloc = GPUAllocator(make_config(jobs_dir=tmp_path / "jobs"))
    info = alloc.get_gpu_info()

    assert len(info) == len(real)
    for gpu, smi in zip(info, real):
        assert gpu["id"] == smi["index"]
        # Total : même carte (marge : arrondis d'API, ~quelques dizaines de Mo).
        assert abs(gpu["memory"]["total"] * 1024 - smi["total_mb"]) < 1024
        # Libre : marge large — le contexte CUDA de CE process et l'activité
        # d'autres process font fluctuer la mesure entre les deux lectures.
        assert abs(gpu["memory"]["free"] * 1024 - smi["free_mb"]) < 3072


def test_manager_and_allocator_share_one_real_snapshot(tmp_path):
    """DoD B3 : snapshot identique entre les deux classes sur la machine multi-GPU réelle.

    Identité et capacité doivent être STRICTEMENT égales ; la mémoire libre peut
    fluctuer entre les deux lectures (autres process) — marge de quelques centaines
    de Mo."""
    from transcria.gpu.vram_manager import VRAMManager
    from transcria.queue.allocator import GPUAllocator

    cfg = make_config(jobs_dir=tmp_path / "jobs")
    manager_view = VRAMManager(cfg).get_gpu_info()
    allocator_view = GPUAllocator(cfg).get_gpu_info()

    assert len(manager_view) == len(allocator_view) >= 1
    for m, a in zip(manager_view, allocator_view):
        assert (m["id"], m["name"]) == (a["id"], a["name"])
        assert m["memory"]["total"] == a["memory"]["total"]
        assert abs(m["memory"]["free"] - a["memory"]["free"]) * 1024 < 512  # Mo


def test_vram_manager_kills_real_listening_process(tmp_path):
    """`_kill_port` (le chemin « processus réel » mort aux tests à fakes) tue
    réellement un processus qui écoute — SIGTERM puis SIGKILL au besoin."""
    from transcria.gpu.vram_manager import VRAMManager

    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        _wait_listening(port)
        vm = VRAMManager(make_config(jobs_dir=tmp_path / "jobs"))

        assert vm._kill_port(port) is True
        proc.wait(timeout=15)
        assert proc.returncode is not None
    finally:
        if proc.poll() is None:
            proc.kill()


def test_supervisor_launches_and_stops_real_stub(tmp_path):
    """Le superviseur de production (planner VRAM réel, lanceur de script réel,
    sonde HTTP réelle) lance un stub, le déclare prêt (CAS B), le retrouve
    résident (CAS A), puis l'arrête via scripts/stop_stt.sh."""
    from transcria.gpu.stt_engine_supervisor import EngineSpec, build_stt_supervisor, http_health_prober

    port = _free_port()
    script = tmp_path / "launch_stt_stub.sh"
    script.write_text(
        "#!/bin/bash\n"
        f'exec {sys.executable} -m http.server "$STT_PORT" --bind 127.0.0.1\n'
    )
    spec = EngineSpec(
        name="stub-smoke",
        script=str(script),
        gpu=0,
        gpu_mem=0.05,
        port=port,
        health_url=f"http://127.0.0.1:{port}/",
    )
    supervisor = build_stt_supervisor(make_config(jobs_dir=tmp_path / "jobs"))
    try:
        result = supervisor.ensure_ready(spec)
        assert result.status == "launched", f"lancement attendu, obtenu {result.status} ({result.reason})"
        assert result.ok

        # Résident → CAS A sans relancer.
        again = supervisor.ensure_ready(spec)
        assert again.status == "ready"

        assert supervisor.stop_engine(spec) is True
        time.sleep(1)
        assert http_health_prober(spec.health_url) is False
    finally:
        # Filet : ne jamais laisser un stub écouter après le test.
        subprocess.run(
            ["bash", "scripts/stop_stt.sh", "--port", str(port)],
            capture_output=True, timeout=60,
        )

import os
import threading

from transcria.gpu.gpu_session import GPUSession
from transcria.queue.allocator import GPUAllocator


def _allocator(tmp_path, free_mb=24000, total_mb=24000):
    cfg = {
        "gpu": {"min_free_vram_mb": 1000},
        "workflow": {
            "scheduling": {
                "pid_file": str(tmp_path / "pids.json"),
                "kill_patterns": ["llama-server"],
            }
        },
    }
    alloc = GPUAllocator(cfg)
    alloc.get_gpu_info = lambda: [
        {
            "id": 0,
            "name": "GPU test",
            "memory": {
                "free": free_mb / 1024,
                "used": (total_mb - free_mb) / 1024,
                "total": total_mb / 1024,
            },
        }
    ]
    return alloc


def test_try_reserve_is_atomic_under_contention(tmp_path):
    alloc = _allocator(tmp_path, free_mb=7000, total_mb=8000)
    results = []
    barrier = threading.Barrier(2)

    def reserve(job_id):
        barrier.wait()
        results.append(alloc.try_reserve(job_id, 5000, "stt"))

    t1 = threading.Thread(target=reserve, args=("job-a",))
    t2 = threading.Thread(target=reserve, args=("job-b",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert sum(item is not None for item in results) == 1
    snapshot = alloc.get_snapshot()
    assert snapshot["gpus"][0]["reserved_vram_mb"] == 5000


def test_allocator_maps_physical_cuda_visible_devices(tmp_path, monkeypatch):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0,2")
    alloc = _allocator(tmp_path)
    alloc.get_gpu_info = lambda: [
        {
            "id": 0,
            "name": "GPU 0",
            "memory": {"free": 10, "used": 14, "total": 24},
        },
        {
            "id": 1,
            "name": "GPU hidden",
            "memory": {"free": 23, "used": 1, "total": 24},
        },
        {
            "id": 2,
            "name": "GPU 2",
            "memory": {"free": 22, "used": 2, "total": 24},
        },
    ]

    reservation = alloc.try_reserve("job-a", 10000, "stt")

    assert reservation is not None
    assert reservation.gpu_index == 1
    snapshot = alloc.get_snapshot()
    assert [gpu["id"] for gpu in snapshot["gpus"]] == [0, 1]
    assert snapshot["gpus"][1]["name"] == "GPU 2"
    assert snapshot["gpus"][1]["reserved_vram_mb"] == 10000


def test_allocator_uses_remapped_torch_gpu_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0,2")
    alloc = _allocator(tmp_path)
    alloc.get_gpu_info = lambda: [
        {
            "id": 0,
            "name": "visible cuda:0",
            "cuda_visible_remapped": True,
            "memory": {"free": 10, "used": 14, "total": 24},
        },
        {
            "id": 1,
            "name": "visible cuda:1",
            "cuda_visible_remapped": True,
            "memory": {"free": 22, "used": 2, "total": 24},
        },
    ]

    reservation = alloc.try_reserve("job-a", 10000, "stt")

    assert reservation is not None
    assert reservation.gpu_index == 1


def test_release_phase_only_releases_matching_job_phase(tmp_path):
    alloc = _allocator(tmp_path, free_mb=24000)
    assert alloc.try_reserve("job-a", 4000, "stt") is not None
    assert alloc.try_reserve("job-a", 2000, "diarization") is not None
    assert alloc.try_reserve("job-b", 3000, "stt") is not None

    alloc.release_phase("job-a", "stt")

    snapshot = alloc.get_snapshot()
    phases = {
        (reservation["job_id"], reservation["phase"])
        for reservation in snapshot["gpus"][0]["reservations"]
    }
    assert phases == {("job-a", "diarization"), ("job-b", "stt")}
    assert snapshot["gpus"][0]["reserved_vram_mb"] == 5000


def test_release_reservations_frees_only_target_job_and_returns_mb(tmp_path):
    """Filet de sécurité : libère TOUTES les réservations d'un job (toutes phases),
    n'touche pas aux autres jobs, et retourne les Mo récupérés."""
    alloc = _allocator(tmp_path, free_mb=24000)
    alloc.try_reserve("job-a", 4000, "stt")
    alloc.try_reserve("job-a", 2000, "diarization")
    alloc.try_reserve("job-b", 3000, "stt")

    reclaimed = alloc.release_reservations("job-a")

    assert reclaimed == 6000  # 4000 + 2000, les deux phases de job-a
    snapshot = alloc.get_snapshot()
    remaining = {(r["job_id"], r["phase"]) for r in snapshot["gpus"][0]["reservations"]}
    assert remaining == {("job-b", "stt")}


def test_release_reservations_is_idempotent_noop(tmp_path):
    """Idempotent : rappelé sur un job déjà libéré (ou inconnu) → 0, aucun effet."""
    alloc = _allocator(tmp_path, free_mb=24000)
    alloc.try_reserve("job-a", 4000, "stt")
    assert alloc.release_reservations("job-a") == 4000
    assert alloc.release_reservations("job-a") == 0   # déjà libéré
    assert alloc.release_reservations("inconnu") == 0  # jamais réservé


def test_release_reservations_does_not_touch_llm_lock(tmp_path):
    """Le filet de sécurité ne doit PAS libérer le verrou LLM (délicat, géré ailleurs) :
    un job concurrent qui tient le verrou le conserve."""
    alloc = _allocator(tmp_path, free_mb=24000)
    alloc.try_reserve("job-a", 4000, "stt")
    assert alloc.try_acquire_llm("job-b") is True   # job-b tient le verrou LLM

    alloc.release_reservations("job-a")             # filet de sécurité sur job-a

    # Le verrou LLM de job-b est intact : un tiers ne peut toujours pas l'acquérir.
    assert alloc.try_acquire_llm("job-c") is False


def test_release_still_frees_reservations_and_llm(tmp_path):
    """Régression : release() complet libère TOUJOURS l'accounting ET le verrou LLM."""
    alloc = _allocator(tmp_path, free_mb=24000)
    alloc.try_reserve("job-a", 4000, "stt")
    assert alloc.try_acquire_llm("job-a") is True

    alloc.release("job-a")

    assert alloc.get_snapshot()["gpus"][0]["reserved_vram_mb"] == 0
    assert alloc.try_acquire_llm("job-b") is True   # verrou LLM bien libéré


def test_gpu_session_releases_only_its_phase(tmp_path):
    alloc = _allocator(tmp_path, free_mb=24000)
    alloc.try_reserve("job-b", 3000, "stt")

    with GPUSession(alloc, "pyannote", 2000, job_id="job-a", phase="diarization") as session:
        assert session.gpu_index == 0
        assert alloc.get_snapshot()["gpus"][0]["reserved_vram_mb"] == 5000

    snapshot = alloc.get_snapshot()
    reservations = snapshot["gpus"][0]["reservations"]
    assert len(reservations) == 1
    assert reservations[0]["job_id"] == "job-b"
    assert reservations[0]["phase"] == "stt"


def test_llm_lock_is_exclusive(tmp_path):
    alloc = _allocator(tmp_path)

    assert alloc.try_acquire_llm("job-a") is True
    assert alloc.try_acquire_llm("job-b") is False
    alloc.release_llm("job-b")
    assert alloc.try_acquire_llm("job-b") is False
    alloc.release_llm("job-a")
    assert alloc.try_acquire_llm("job-b") is True


def test_pid_tracking_persists_and_reloads_alive_process(tmp_path):
    alloc = _allocator(tmp_path)
    alloc.register_pid(os.getpid(), "test-process")

    reloaded = _allocator(tmp_path)
    snapshot = reloaded.get_snapshot()

    assert snapshot["tracked_pids"] == 1
    reloaded.unregister_pid(os.getpid())

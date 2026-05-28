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


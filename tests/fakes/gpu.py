"""Fakes GPU — l'inventaire nvidia-smi simulé et la session VRAM d'arbitrage."""


def fake_gpu_info(*gpus: dict) -> list[dict]:
    """Inventaire à la forme de ``GPUAllocator.get_gpu_info()`` (mémoire en Go).

    Chaque GPU se décrit par ``free_mb``/``total_mb`` (et ``id``/``name``
    optionnels) : ``fake_gpu_info({"free_mb": 24_000, "total_mb": 32_000})``.
    """
    info = []
    for i, gpu in enumerate(gpus or ({"free_mb": 24_000, "total_mb": 32_000},)):
        free_mb = float(gpu.get("free_mb", 24_000))
        total_mb = float(gpu.get("total_mb", 32_000))
        info.append({
            "id": int(gpu.get("id", i)),
            "name": str(gpu.get("name", f"GPU fake {i}")),
            "memory": {
                "free": free_mb / 1024,
                "used": (total_mb - free_mb) / 1024,
                "total": total_mb / 1024,
            },
        })
    return info


class FakeArbitrageVram:
    """Surface VRAM lue par le pipeline en fin de traitement (arrêt LLM d'arbitrage)."""

    def __init__(self, *, arbitrage_running: bool = False):
        self.arbitrage_running = arbitrage_running
        self.stop_calls = 0

    def is_arbitrage_llm_running(self) -> bool:
        return self.arbitrage_running

    def stop_arbitrage_llm(self) -> None:
        self.stop_calls += 1
        self.arbitrage_running = False

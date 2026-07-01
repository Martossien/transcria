"""VRAMManager × backend Ollama (Axe A) — délégation du cycle de vie, GPU-free.

On injecte un faux backend : le VRAMManager ne doit ni lancer de script, ni tuer de
process pour Ollama — il DÉLÈGUE (charger/décharger/sonder) au backend, et n'arrête
jamais le démon persistant.
"""
from transcria.gpu.vram_manager import VRAMManager


class _FakeOllamaBackend:
    backend_type = "ollama"

    def __init__(self):
        self.calls = []
        self._loaded = True

    def is_loaded(self):
        self.calls.append("is_loaded")
        return self._loaded

    def unload(self):
        self.calls.append("unload")
        self._loaded = False
        return True

    def ensure_available(self):
        self.calls.append("ensure_available")
        self._loaded = True
        return True


def _ollama_vm():
    cfg = {
        "services": {"backend": "ollama", "ollama_url": "http://127.0.0.1:11434"},
        "workflow": {"arbitration_llm": {"model_id": "qwen3:8b"}},
    }
    vm = VRAMManager(cfg)
    fake = _FakeOllamaBackend()
    vm._backend = fake  # injecte le backend (pas de démon réel)
    return vm, fake


def test_backend_is_ollama_detected():
    vm, _ = _ollama_vm()
    assert vm._backend_is_ollama() is True


def test_is_running_delegates_to_is_loaded():
    vm, fake = _ollama_vm()
    assert vm.is_arbitrage_llm_running() is True
    assert "is_loaded" in fake.calls


def test_stop_delegates_to_unload_and_never_kills_daemon():
    vm, fake = _ollama_vm()
    assert vm.stop_arbitrage_llm() is True
    assert "unload" in fake.calls
    # Après déchargement, la sonde le voit libéré (démon toujours vivant côté réel).
    assert vm.is_arbitrage_llm_running() is False


def test_launch_delegates_to_ensure_available_no_script():
    vm, fake = _ollama_vm()
    # Aucun arbitrage_script sur disque : le chemin llama.cpp échouerait ; Ollama délègue.
    assert vm.launch_arbitrage_llm() is True
    assert "ensure_available" in fake.calls


def test_ensure_ready_delegates_to_ensure_available():
    vm, fake = _ollama_vm()
    assert vm.ensure_arbitrage_llm_ready() is True
    assert "ensure_available" in fake.calls


def test_daemon_never_a_kill_target_even_if_pattern_configured():
    cfg = {"workflow": {"scheduling": {"kill_patterns": ["ollama", "vllm"]}}}
    vm = VRAMManager(cfg)
    # Le garde-fou l'emporte sur le pattern : jamais de kill du démon Ollama.
    assert vm._matches_kill_pattern("ollama") is False
    assert vm._matches_kill_pattern("/usr/local/bin/ollama") is False
    # Les vrais moteurs mono-modèle restent des cibles légitimes.
    assert vm._matches_kill_pattern("vllm serve") is True

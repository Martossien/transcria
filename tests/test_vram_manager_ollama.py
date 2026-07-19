"""VRAMManager × backend Ollama (Axe A) — délégation du cycle de vie, GPU-free.

On injecte un faux backend : le VRAMManager ne doit ni lancer de script, ni tuer de
process pour Ollama — il DÉLÈGUE (charger/décharger/sonder) au backend, et n'arrête
jamais le démon persistant.
"""
from transcria.gpu.vram_manager import VRAMManager, should_recalibrate


class _FakeOllamaBackend:
    backend_type = "ollama"

    def __init__(self, measured=None):
        self.calls = []
        self._loaded = True
        self._measured = measured

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

    def measured_vram_mb(self):
        self.calls.append("measured_vram_mb")
        return self._measured


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


def test_launch_triggers_vram_recalibration():
    # « Vérif au 1ᵉʳ load » : le chargement via launch_arbitrage_llm doit recaler la VRAM.
    vm, fake = _ollama_vm()
    fake._measured = 28000
    vm.llm_vram_mb = 60000
    vm.launch_arbitrage_llm()
    assert "measured_vram_mb" in fake.calls and vm.llm_vram_mb == 28000


def test_ensure_ready_delegates_to_ensure_available():
    vm, fake = _ollama_vm()
    assert vm.ensure_arbitrage_llm_ready() is True
    assert "ensure_available" in fake.calls


class TestRecalibration:
    def test_should_recalibrate_threshold(self):
        assert should_recalibrate(24000, 14700) is True        # écart net → recaler
        assert should_recalibrate(14700, 15000) is False        # ~2 % → non
        assert should_recalibrate(0, 14700) is True             # pas de valeur courante
        assert should_recalibrate(14700, 0) is False            # mesure absente → garder

    def test_first_load_adopts_measured(self):
        vm, fake = _ollama_vm()
        fake._measured = 14700
        vm.llm_vram_mb = 60000            # empreinte calculée grossière
        vm.recalibrate_llm_vram_from_measurement()
        assert vm.llm_vram_mb == 14700    # la mesure prime
        assert "measured_vram_mb" in fake.calls

    def test_recalibration_is_once(self):
        vm, fake = _ollama_vm()
        fake._measured = 14700
        vm.recalibrate_llm_vram_from_measurement()
        fake.calls.clear()
        vm.recalibrate_llm_vram_from_measurement()   # 2ᵉ appel = no-op (déjà recalé)
        assert fake.calls == []

    def test_no_change_when_measurement_absent(self):
        vm, fake = _ollama_vm()
        fake._measured = None
        vm.llm_vram_mb = 60000
        vm.recalibrate_llm_vram_from_measurement()
        assert vm.llm_vram_mb == 60000    # rien à recaler → inchangé


def test_daemon_never_a_kill_target_even_if_pattern_configured():
    cfg = {"workflow": {"scheduling": {"kill_patterns": ["ollama", "vllm"]}}}
    vm = VRAMManager(cfg)
    # Le garde-fou l'emporte sur le pattern : jamais de kill du démon Ollama.
    assert vm._matches_kill_pattern("ollama") is False
    assert vm._matches_kill_pattern("/usr/local/bin/ollama") is False
    # Les vrais moteurs mono-modèle restent des cibles légitimes.
    assert vm._matches_kill_pattern("vllm serve") is True


class TestPersistPlacementGuard:
    """Vécu 2026-07-19 : _persist_llm_vram_mb inventait `[0]` sans llm_gpu_indices,
    écrasant une calibration bi-GPU réelle (49000/[0,1] → 14700/[0])."""

    def test_sans_indices_ne_persiste_jamais(self, tmp_path, monkeypatch):
        import yaml as pyyaml

        from transcria.gpu.vram_manager import VRAMManager

        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("gpu:\n  llm_vram_mb: 49000\n", encoding="utf-8")
        monkeypatch.setenv("TRANSCRIA_CONFIG", str(cfg_file))

        vm = VRAMManager({"gpu": {"llm_vram_mb": 49000}})   # PAS d'indices déclarés
        vm._persist_llm_vram_mb(14700)

        data = pyyaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        assert data["gpu"]["llm_vram_mb"] == 49000           # fichier INTACT
        assert "llm_gpu_indices" not in data["gpu"]

    def test_avec_indices_persiste_et_repartit(self, tmp_path, monkeypatch):
        import yaml as pyyaml

        from transcria.gpu.vram_manager import VRAMManager

        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("gpu:\n  llm_vram_mb: 10000\n  llm_gpu_indices: [0, 1]\n", encoding="utf-8")
        monkeypatch.setenv("TRANSCRIA_CONFIG", str(cfg_file))

        vm = VRAMManager({"gpu": {"llm_vram_mb": 10000, "llm_gpu_indices": [0, 1]}})
        vm._persist_llm_vram_mb(14700)

        data = pyyaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        assert data["gpu"]["llm_vram_mb"] == 14700
        assert data["gpu"]["llm_gpu_indices"] == [0, 1]      # placement PRÉSERVÉ

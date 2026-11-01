"""VRAMManager × backend CLOUD (lot A LLM cloud) — délégation au fournisseur, GPU-free.

En cloud, ce process ne lance, n'arrête, ne réserve ni ne sonde localement RIEN : la seule
autorité est le fournisseur (``GET {base}/models`` avec la clé, en https).
"""
import requests
from builders import make_config

from transcria.gpu import vram_manager as vm_mod
from transcria.gpu.llm_backend import CloudLLMBackend
from transcria.gpu.vram_manager import VRAMManager


def _cloud_cfg(**services):
    return make_config({
        "services": {"backend": "cloud", **services},
        "workflow": {
            "arbitration_llm": {"enabled": True, "model_id": "mistral/mistral-large-4"},
            "summary_llm": {"enabled": True, "model_id": "mistral/mistral-large-4"},
        },
    })


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {"data": [{"id": "autre-modele"}]}
        self.text = "" if status == 200 else "clé invalide"

    def json(self):
        return self._payload


def _boom(*_a, **_k):
    raise AssertionError("aucun geste local (process, port, GPU) n'est permis en cloud")


def _forbid_local_gestures(monkeypatch, vm):
    monkeypatch.setattr(vm_mod.subprocess, "run", _boom)
    monkeypatch.setattr(vm_mod.subprocess, "Popen", _boom)
    monkeypatch.setattr(vm_mod, "kill_port_listeners", _boom)
    monkeypatch.setattr(vm_mod, "release_idle_vram", _boom)
    monkeypatch.setattr(vm, "_free_memory", _boom)
    monkeypatch.setattr(vm, "_kill_port", _boom)
    monkeypatch.setattr(vm, "_preflight_llm_vram", _boom)
    monkeypatch.setattr(requests, "post", _boom)  # jamais de test-inférence en cloud


def _record_gets(monkeypatch, response):
    urls: list[str] = []

    def fake_get(url, timeout=5, **kwargs):
        urls.append(url)
        assert kwargs.get("headers", {}).get("Authorization") == "Bearer cle-de-test"
        return response

    monkeypatch.setattr(requests, "get", fake_get)
    return urls


def test_backend_cloud_detecte(monkeypatch):
    vm = VRAMManager(_cloud_cfg())
    assert vm._backend_is_cloud() is True
    assert vm._backend_is_ollama() is False
    assert isinstance(vm._arbitrage_backend(), CloudLLMBackend)


def test_is_running_et_ensure_delegent_au_fournisseur(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "cle-de-test")
    vm = VRAMManager(_cloud_cfg())
    _forbid_local_gestures(monkeypatch, vm)
    urls = _record_gets(monkeypatch, _Resp(200))
    assert vm.is_arbitrage_llm_running() is True
    # Modèle attendu ≠ data[0] : AUCUNE comparaison en cloud (le fournisseur liste son catalogue).
    assert vm.ensure_arbitrage_llm_ready(expected_model_id="arbitrage") is True
    assert urls == ["https://api.mistral.ai/v1/models"] * 2


def test_ensure_fournisseur_injoignable_sans_lancement(monkeypatch, caplog):
    monkeypatch.setenv("MISTRAL_API_KEY", "cle-de-test")
    vm = VRAMManager(_cloud_cfg())
    _forbid_local_gestures(monkeypatch, vm)
    monkeypatch.setattr(vm, "launch_arbitrage_llm", _boom)
    monkeypatch.setattr(vm, "stop_arbitrage_llm", _boom)
    _record_gets(monkeypatch, _Resp(401))
    with caplog.at_level("ERROR"):
        assert vm.ensure_arbitrage_llm_ready(expected_model_id=None) is False
        assert vm.is_arbitrage_llm_running() is False
    assert "LLM cloud mistral injoignable" in caplog.text
    assert "cle-de-test" not in caplog.text


def test_ensure_cle_absente_message_lisible(monkeypatch, caplog):
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    vm = VRAMManager(_cloud_cfg())
    _forbid_local_gestures(monkeypatch, vm)
    monkeypatch.setattr(requests, "get", _boom)  # clé absente : aucune requête partie
    with caplog.at_level("ERROR"):
        assert vm.ensure_arbitrage_llm_ready() is False
    assert "MISTRAL_API_KEY" in caplog.text


def test_launch_refuse_et_stop_noop(monkeypatch, caplog):
    vm = VRAMManager(_cloud_cfg())
    _forbid_local_gestures(monkeypatch, vm)
    monkeypatch.setattr(requests, "get", _boom)
    with caplog.at_level("ERROR"):
        assert vm.launch_arbitrage_llm() is False
    assert "backend cloud" in caplog.text
    assert vm.stop_arbitrage_llm() is True
    assert vm.stop_cleanup_llm_ports() is True


def test_recalibrage_vram_jamais_en_cloud(monkeypatch):
    vm = VRAMManager(_cloud_cfg())
    monkeypatch.setattr(vm, "_arbitrage_backend", _boom)
    monkeypatch.setattr(vm, "_persist_llm_vram_mb", _boom)
    vm.recalibrate_llm_vram_from_measurement()


def test_cloud_gagne_malgre_les_residus_locaux(monkeypatch):
    """ollama_url / arbitrage_script hérités de la config locale : ignorés en cloud."""
    monkeypatch.setenv("MISTRAL_API_KEY", "cle-de-test")
    cfg = _cloud_cfg(ollama_url="http://127.0.0.1:11434",
                     arbitrage_script="./scripts/launch_arbitrage.sh")
    vm = VRAMManager(cfg)
    _forbid_local_gestures(monkeypatch, vm)
    urls = _record_gets(monkeypatch, _Resp(200))
    assert isinstance(vm._arbitrage_backend(), CloudLLMBackend)
    assert vm._backend_is_ollama() is False
    assert vm.is_arbitrage_llm_running() is True
    assert vm.ensure_arbitrage_llm_ready("arbitrage") is True
    assert all(u.startswith("https://api.mistral.ai/") for u in urls)
    assert vm.launch_arbitrage_llm() is False

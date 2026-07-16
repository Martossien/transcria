"""Kill patterns uniques (gpu/kill_patterns.py — vague B3).

Vérifie le contrat qui a motivé la fusion : même clé de config → mêmes patterns
pour les deux classes, et la protection Ollama (que seul VRAMManager portait)
vaut désormais AUSSI pour l'allocateur.
"""
from transcria.gpu.kill_patterns import (
    DEFAULT_KILL_PATTERNS,
    kill_patterns_from_config,
    matches_kill_pattern,
)


class TestBuild:
    def test_defaults_when_key_absent(self):
        assert kill_patterns_from_config({}) == DEFAULT_KILL_PATTERNS

    def test_config_overrides_lowercased_and_blank_dropped(self):
        cfg = {"workflow": {"scheduling": {"kill_patterns": ["VLLM", "  ", "Mon-Serveur"]}}}
        assert kill_patterns_from_config(cfg) == ("vllm", "mon-serveur")

    def test_none_sections_tolerated(self):
        assert kill_patterns_from_config({"workflow": {"scheduling": None}}) == DEFAULT_KILL_PATTERNS


class TestMatch:
    def test_matches_configured_pattern(self):
        assert matches_kill_pattern("vllm serve --model x", ("vllm",)) is True
        assert matches_kill_pattern("python train.py", ("vllm",)) is False

    def test_ollama_is_never_killable_even_if_covered(self):
        # La divergence historique : l'allocateur ne protégeait PAS le démon.
        assert matches_kill_pattern("ollama", ("ollama", "vllm")) is False
        assert matches_kill_pattern("/usr/local/bin/ollama serve", ("ollama",)) is False

    def test_case_insensitive(self):
        assert matches_kill_pattern("LLAMA-SERVER --port 8080", ("llama-server",)) is True


class TestBothClassesShareTheImplementation:
    def test_allocator_gains_the_ollama_protection(self, tmp_path):
        from builders import make_config

        from transcria.queue.allocator import GPUAllocator

        cfg = make_config(
            {"workflow": {"scheduling": {"kill_patterns": ["ollama", "vllm"]}}},
            jobs_dir=tmp_path / "jobs",
        )
        alloc = GPUAllocator(cfg)
        assert alloc._match_kill_pattern("ollama") is False       # protégé désormais
        assert alloc._match_kill_pattern("vllm serve") is True

    def test_manager_and_allocator_read_the_same_key(self, tmp_path):
        from builders import make_config

        from transcria.gpu.vram_manager import VRAMManager
        from transcria.queue.allocator import GPUAllocator

        cfg = make_config(
            {"workflow": {"scheduling": {"kill_patterns": ["Mon-Serveur"]}}},
            jobs_dir=tmp_path / "jobs",
        )
        assert VRAMManager(cfg)._kill_patterns == GPUAllocator(cfg)._kill_patterns == ("mon-serveur",)

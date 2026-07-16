"""Niveau 2 de l'échelle llama.cpp (Axe C) — binaires CUDA précompilés ai-dock.

GPU-free et SANS réseau : on teste la logique PURE de sélection d'artefact (politique
« nearest »), le parsing des noms, et la vérification de checksum. L'I/O réseau
(install_prebuilt_llama) est exercée à l'E2E GPU, pas ici.
"""
import hashlib

from transcria.installer.arbitrage import (
    normalize_arch,
    parse_prebuilt_artifact,
    prebuilt_artifact_name,
    select_prebuilt_artifact,
    sha256_of_file,
    verify_sha256,
)

_AVAILABLE = [
    "llama.cpp-b9851-cuda-12.8-amd64.tar.gz",
    "llama.cpp-b9851-cuda-12.8-arm64.tar.gz",
    "llama.cpp-b9840-cuda-12.8-amd64.tar.gz",
    "llama.cpp-b9860-cuda-12.8-amd64.tar.gz",
    "some-readme.txt",
]


class TestNaming:
    def test_artifact_name(self):
        assert prebuilt_artifact_name(9851) == "llama.cpp-b9851-cuda-12.8-amd64.tar.gz"
        assert prebuilt_artifact_name(9851, cuda="12.6", arch="arm64") == "llama.cpp-b9851-cuda-12.6-arm64.tar.gz"

    def test_parse_roundtrip(self):
        assert parse_prebuilt_artifact("llama.cpp-b9851-cuda-12.8-amd64.tar.gz") == (9851, "12.8", "amd64")

    def test_parse_rejects_foreign(self):
        assert parse_prebuilt_artifact("some-readme.txt") is None
        assert parse_prebuilt_artifact("llama.cpp-b9851-vulkan-amd64.tar.gz") is None

    def test_normalize_arch(self):
        assert normalize_arch("x86_64") == "amd64"
        assert normalize_arch("aarch64") == "arm64"
        assert normalize_arch("weird") == "amd64"  # défaut prudent


class TestNearestPolicy:
    def test_exact_build_preferred(self):
        assert select_prebuilt_artifact(_AVAILABLE, wanted_build=9851) == "llama.cpp-b9851-cuda-12.8-amd64.tar.gz"

    def test_nearest_newer_when_exact_absent(self):
        # 9855 absent → plus proche SUPÉRIEUR = 9860 (pas 9851).
        assert select_prebuilt_artifact(_AVAILABLE, wanted_build=9855) == "llama.cpp-b9860-cuda-12.8-amd64.tar.gz"

    def test_falls_back_to_latest_older_when_no_newer(self):
        # 9999 > tout → repli sur le plus récent disponible (9860).
        assert select_prebuilt_artifact(_AVAILABLE, wanted_build=9999) == "llama.cpp-b9860-cuda-12.8-amd64.tar.gz"

    def test_respects_arch_filter(self):
        assert select_prebuilt_artifact(_AVAILABLE, wanted_build=9851, arch="arm64") == "llama.cpp-b9851-cuda-12.8-arm64.tar.gz"

    def test_none_when_cuda_absent(self):
        assert select_prebuilt_artifact(_AVAILABLE, wanted_build=9851, cuda="11.8") is None

    def test_none_when_empty(self):
        assert select_prebuilt_artifact([], wanted_build=9851) is None


class TestChecksum:
    def test_verify_matches(self, tmp_path):
        f = tmp_path / "a.tar.gz"
        f.write_bytes(b"hello-binary")
        expected = hashlib.sha256(b"hello-binary").hexdigest()
        assert sha256_of_file(f) == expected
        assert verify_sha256(f, expected) is True
        assert verify_sha256(f, expected.upper()) is True  # insensible à la casse

    def test_verify_rejects_mismatch(self, tmp_path):
        f = tmp_path / "a.tar.gz"
        f.write_bytes(b"hello-binary")
        assert verify_sha256(f, "deadbeef") is False

    def test_empty_expected_is_refused(self, tmp_path):
        # Pas de checksum = pas de confiance : on refuse (source tierce).
        f = tmp_path / "a.tar.gz"
        f.write_bytes(b"x")
        assert verify_sha256(f, "") is False

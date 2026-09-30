"""Niveau 2 de l'échelle llama.cpp (Axe C) — binaires CUDA précompilés ai-dock.

GPU-free et SANS réseau : on teste la logique PURE de sélection d'artefact (tag exact,
schémas `vX.Y.Z` et `bNNNN`), le parsing des noms, et la vérification de checksum. L'I/O réseau
(install_prebuilt_llama) est exercée à l'E2E GPU, pas ici.
"""
import hashlib

from transcria.installer.arbitrage import (
    normalize_arch,
    normalize_release_tag,
    parse_prebuilt_artifact,
    prebuilt_artifact_name,
    select_prebuilt_artifact,
    sha256_of_file,
    verify_sha256,
)

_AVAILABLE = [
    "llama.cpp-v0.5.0-cuda-12.8-amd64.tar.gz",
    "llama.cpp-v0.5.0-cuda-12.8-arm64.tar.gz",
    "llama.cpp-b9851-cuda-12.8-amd64.tar.gz",
    "llama.cpp-b9860-cuda-12.8-amd64.tar.gz",
    "some-readme.txt",
]


class TestNaming:
    def test_artifact_name_semver(self):
        assert prebuilt_artifact_name("v0.5.0") == "llama.cpp-v0.5.0-cuda-12.8-amd64.tar.gz"
        assert prebuilt_artifact_name("v0.5.0", cuda="12.6", arch="arm64") == "llama.cpp-v0.5.0-cuda-12.6-arm64.tar.gz"

    def test_artifact_name_legacy_build_counter(self):
        # L'ancien schéma `bNNNN` reste servi — entier nu, chaîne de chiffres ou tag complet.
        assert prebuilt_artifact_name(9851) == "llama.cpp-b9851-cuda-12.8-amd64.tar.gz"
        assert prebuilt_artifact_name("9851") == prebuilt_artifact_name("b9851")

    def test_parse_roundtrip(self):
        assert parse_prebuilt_artifact("llama.cpp-v0.5.0-cuda-12.8-amd64.tar.gz") == ("v0.5.0", "12.8", "amd64")
        assert parse_prebuilt_artifact("llama.cpp-b9851-cuda-12.8-amd64.tar.gz") == ("b9851", "12.8", "amd64")

    def test_parse_rejects_foreign(self):
        assert parse_prebuilt_artifact("some-readme.txt") is None
        assert parse_prebuilt_artifact("llama.cpp-b9851-vulkan-amd64.tar.gz") is None

    def test_normalize_arch(self):
        assert normalize_arch("x86_64") == "amd64"
        assert normalize_arch("aarch64") == "arm64"
        assert normalize_arch("weird") == "amd64"  # défaut prudent


class TestReleaseTag:
    def test_accepts_both_upstream_schemes(self):
        assert normalize_release_tag("v0.5.0") == "v0.5.0"
        assert normalize_release_tag("b9851") == "b9851"
        assert normalize_release_tag(9851) == "b9851"
        assert normalize_release_tag(" 9851 ") == "b9851"

    def test_rejects_anything_else(self):
        # Le tag finit dans une URL d'API : ni devinette, ni séparateur de chemin.
        for bad in ("", "latest", "v0.5", "0.5.0", "b", "v0.5.0/../x", "b9851;rm"):
            assert normalize_release_tag(bad) is None


class TestExactSelection:
    def test_exact_tag(self):
        assert select_prebuilt_artifact(_AVAILABLE, wanted_tag="v0.5.0") == "llama.cpp-v0.5.0-cuda-12.8-amd64.tar.gz"
        assert select_prebuilt_artifact(_AVAILABLE, wanted_tag=9851) == "llama.cpp-b9851-cuda-12.8-amd64.tar.gz"

    def test_no_neighbour_substitution(self):
        # Le sha256 épinglé ne vaut que pour l'archive demandée : pas de « plus proche ».
        assert select_prebuilt_artifact(_AVAILABLE, wanted_tag="v0.4.1") is None
        assert select_prebuilt_artifact(_AVAILABLE, wanted_tag=9855) is None

    def test_respects_arch_filter(self):
        assert select_prebuilt_artifact(_AVAILABLE, wanted_tag="v0.5.0", arch="arm64") == "llama.cpp-v0.5.0-cuda-12.8-arm64.tar.gz"

    def test_none_when_cuda_absent(self):
        assert select_prebuilt_artifact(_AVAILABLE, wanted_tag="v0.5.0", cuda="11.8") is None

    def test_none_when_empty(self):
        assert select_prebuilt_artifact([], wanted_tag="v0.5.0") is None

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

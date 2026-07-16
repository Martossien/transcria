"""Phase installeur parakeetcpp : patron rétréci d'audiocpp (pas de venv, pas de
config JSON) — idempotence par COMMIT, submodules, arch CUDA native."""
from pathlib import Path

import pytest

from fakes import FakeConsole

from transcria.installer.parakeetcpp_phase import (
    PARAKEETCPP_PINNED_COMMIT,
    ParakeetcppPhaseError,
    ParakeetcppPlan,
    apply_parakeetcpp,
)


def _complete(home: Path, commit: str) -> None:
    (home / "bin").mkdir(parents=True)
    b = home / "bin" / "parakeet-server"
    b.write_bytes(b"#!/bin/sh\n")
    b.chmod(0o755)
    (home / "COMMIT").write_text(commit + "\n")


def test_noop_si_complet(tmp_path):
    _complete(tmp_path / "parakeetcpp", PARAKEETCPP_PINNED_COMMIT)
    calls = []
    apply_parakeetcpp(ParakeetcppPlan(runtimes_dir=tmp_path),
                      console=FakeConsole(), runner=lambda cmd, cwd=None: calls.append(cmd))
    assert calls == []


def test_sequence_epinglee_avec_submodules_et_arch_native(tmp_path):
    home = tmp_path / "parakeetcpp"
    calls = []

    def runner(cmd, cwd=None):
        calls.append(" ".join(cmd))
        if cmd[0] == "cmake" and "--build" in cmd:
            built = home / "src" / "build" / "examples" / "server" / "parakeet-server"
            built.parent.mkdir(parents=True, exist_ok=True)
            built.write_bytes(b"bin")
            built.chmod(0o755)

    apply_parakeetcpp(ParakeetcppPlan(runtimes_dir=tmp_path),
                      console=FakeConsole(), runner=runner)
    assert any(c.startswith("git clone --recursive") for c in calls)
    assert any("checkout" in c and PARAKEETCPP_PINNED_COMMIT in c for c in calls)
    assert any("submodule update --init --recursive" in c for c in calls)
    assert any("CMAKE_CUDA_ARCHITECTURES=native" in c for c in calls)   # piège arch vécu
    assert any("PARAKEET_GGML_CUDA=ON" in c for c in calls)
    assert (home / "COMMIT").read_text().strip() == PARAKEETCPP_PINNED_COMMIT


def test_echec_build_erreur_typee(tmp_path):
    def runner(cmd, cwd=None):
        if cmd[0] == "cmake":
            raise RuntimeError("boom")

    with pytest.raises(ParakeetcppPhaseError, match="compilation"):
        apply_parakeetcpp(ParakeetcppPlan(runtimes_dir=tmp_path),
                          console=FakeConsole(), runner=runner)

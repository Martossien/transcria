"""Tests purs de la qualification du binaire llama-server (sans binaire ni GPU)."""
from __future__ import annotations

from transcria.gpu.llama_runtime import (
    MIN_BUILD,
    detect_cuda,
    evaluate_runtime,
    parse_git_describe,
    parse_ldd_output,
    parse_version_output,
)

# Sortie réelle observée le 16/06/2026 (binaire AUTHENTIQUEMENT b9632, cf. test ci-dessous).
_REAL_VERSION = "version: 579 (8edaca9)\nbuilt with GNU 14.2.1 for Linux x86_64\n"

_LDD_OK = """\
\tlinux-vdso.so.1 (0x00007ffd)
\tlibggml-cuda.so.0 => /home/u/.conda/envs/ik_build/lib/libggml-cuda.so.0 (0x00007f44)
\tlibcudart.so.13 => /home/u/.conda/envs/ik_build/lib/libcudart.so.13 (0x00007f44)
\tlibstdc++.so.6 => /home/u/.conda/envs/ik_build/lib/libstdc++.so.6 (0x00007f44)
\t/lib64/ld-linux-x86-64.so.2 (0x00007f44)
"""

_LDD_MISSING = """\
\tlibggml-cuda.so.0 => /home/u/.conda/envs/ik_build/lib/libggml-cuda.so.0 (0x00007f44)
\tlibcudart.so.13 => not found
\tlibcublas.so.13 => not found
"""


# ── Parseurs ────────────────────────────────────────────────────────────────


def test_parse_version_real_output():
    assert parse_version_output(_REAL_VERSION) == (579, "8edaca9")


def test_parse_version_no_commit_and_garbage():
    assert parse_version_output("version: 9632") == (9632, None)
    assert parse_version_output("") == (None, None)
    assert parse_version_output(None) == (None, None)
    assert parse_version_output("llama-server: command not found") == (None, None)


def test_parse_git_describe_variants():
    assert parse_git_describe("b9632-4-g8edaca9") == (9632, 4, "8edaca9")
    assert parse_git_describe("b9632\n") == (9632, 0, None)
    assert parse_git_describe("v1.2.3-5-gabcdef") == (None, 0, None)
    assert parse_git_describe(None) == (None, 0, None)


def test_parse_ldd_resolved_and_missing():
    resolved, missing = parse_ldd_output(_LDD_OK)
    assert "libggml-cuda.so.0" in resolved
    assert resolved["libcudart.so.13"] == "/home/u/.conda/envs/ik_build/lib/libcudart.so.13"
    assert missing == []

    resolved, missing = parse_ldd_output(_LDD_MISSING)
    assert missing == ["libcudart.so.13", "libcublas.so.13"]


def test_detect_cuda():
    assert detect_cuda(["libggml-cuda.so.0", "libstdc++.so.6"]) is True
    assert detect_cuda(["libcublas.so.13"]) is True
    assert detect_cuda(["libstdc++.so.6", "libgomp.so.1"]) is False


# ── evaluate_runtime ─────────────────────────────────────────────────────────


def _eval(**over):
    base = dict(
        path="/x/llama-server",
        version_build=None,
        version_commit=None,
        describe_build=None,
        describe_ahead=0,
        describe_commit=None,
        missing_libs=[],
        has_cuda=True,
    )
    base.update(over)
    return evaluate_runtime(**base)


def test_missing_libs_is_critical():
    r = _eval(missing_libs=["libcudart.so.13"])
    assert r.level == "critical"
    assert r.usable is False
    assert any("NE SE CHARGERA PAS" in f.message for f in r.findings)


def test_git_build_below_min_is_warn_not_blocking():
    """Version trop vieille (même confirmée par git) = WARN : le besoin est relatif au
    modèle, et seules les libs manquantes empêchent le binaire de démarrer."""
    r = _eval(describe_build=9000)
    assert r.level == "warn"
    assert r.usable is True
    assert r.build_source == "git"


def test_only_missing_libs_is_critical():
    """Garde-fou : le SEUL cas critical/inutilisable est une lib manquante."""
    assert _eval(describe_build=9000).usable is True  # version vieille
    assert _eval(version_build=100).usable is True  # self-report très bas
    assert _eval(has_cuda=False, describe_build=9632).usable is True  # CPU-only
    assert _eval(missing_libs=["libcudart.so.13"]).usable is False  # libs ⇒ critical


def test_git_build_ok():
    r = _eval(describe_build=9632, describe_ahead=4)
    assert r.level == "ok"
    assert r.usable is True
    assert r.resolved_build == 9632


def test_b9632_reports_579_trap_is_warn_not_rejection():
    """Le piège : self-report 579 SANS arbre git → warn, jamais un rejet."""
    r = _eval(version_build=579, version_commit="8edaca9")
    assert r.usable is True  # on n'écarte pas un binaire qui marche sur un numéro faux
    assert r.level == "warn"
    assert r.build_source == "self-report"


def test_git_overrides_misleading_self_report():
    """Cas réel complet : git dit b9632, le self-report dit 579 → git fait foi, OK."""
    r = _eval(version_build=579, version_commit="8edaca9", describe_build=9632, describe_ahead=4)
    assert r.usable is True
    assert r.level == "ok"
    assert r.build_source == "git"
    assert r.resolved_build == 9632


def test_self_report_high_is_trusted_ok():
    r = _eval(version_build=MIN_BUILD + 50)
    assert r.level == "ok"
    assert r.build_source == "self-report"


def test_unknown_version_is_warn():
    r = _eval()
    assert r.level == "warn"
    assert r.build_source == "unknown"


def test_cpu_only_build_is_warn():
    r = _eval(describe_build=9632, has_cuda=False)
    assert r.usable is True
    assert r.level == "warn"
    assert any("sans CUDA" in f.message for f in r.findings)


def test_cpu_only_accepted_when_not_expecting_cuda():
    r = evaluate_runtime(
        path="/x/llama-server",
        version_build=None,
        version_commit=None,
        describe_build=9632,
        describe_ahead=0,
        describe_commit=None,
        missing_libs=[],
        has_cuda=False,
        expects_cuda=False,
    )
    assert r.level == "ok"

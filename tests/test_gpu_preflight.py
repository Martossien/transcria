"""Tests du preflight GPU du quickstart Docker (transcria.deploy.gpu_preflight)."""
from __future__ import annotations

from transcria.deploy import gpu_preflight as gp


# --- classify_gpu : verdict par GPU ------------------------------------------------------
def test_classify_gpu_compute_too_low_fails():
    status, msg = gp.classify_gpu(7.0, 32_000)  # Volta : compute 7.0 < 7.5
    assert status == gp.FAIL
    assert "compute" in msg.lower()


def test_classify_gpu_pascal_fails():
    status, _ = gp.classify_gpu(6.1, 24_000)  # Pascal 10xx
    assert status == gp.FAIL


def test_classify_gpu_vram_too_low_fails():
    status, msg = gp.classify_gpu(8.6, 8_000)  # Ampere 8 Go
    assert status == gp.FAIL
    assert "vram" in msg.lower()


def test_classify_gpu_borderline_vram_warns():
    status, _ = gp.classify_gpu(7.5, 11_800)  # ≥ MIN mais < recommandé
    assert status == gp.WARN


def test_classify_gpu_ok():
    status, _ = gp.classify_gpu(8.9, 24_000)  # Ada 24 Go
    assert status == gp.OK


def test_classify_gpu_turing_12gb_ok():
    status, _ = gp.classify_gpu(7.5, 12_288)  # RTX 20xx / T4-ish, pile la limite
    assert status == gp.OK


# --- parse_nvidia_smi_csv ----------------------------------------------------------------
def test_parse_single_gpu():
    assert gp.parse_nvidia_smi_csv("8.9, 24564\n") == [(8.9, 24564)]


def test_parse_multi_gpu_and_blank_lines():
    out = "7.5, 12288\n\n9.0, 81920\n"
    assert gp.parse_nvidia_smi_csv(out) == [(7.5, 12288), (9.0, 81920)]


def test_parse_ignores_unparsable_lines():
    out = "No devices were found\n8.6, 16000\n"
    assert gp.parse_nvidia_smi_csv(out) == [(8.6, 16000)]


def test_parse_empty():
    assert gp.parse_nvidia_smi_csv("") == []


# --- evaluate : verdict global -----------------------------------------------------------
def test_evaluate_no_gpu_fails():
    status, msg = gp.evaluate([])
    assert status == gp.FAIL
    assert "aucun gpu" in msg.lower()


def test_evaluate_best_gpu_wins():
    # une carte incompatible + une compatible ⇒ OK (il suffit qu'un GPU convienne)
    status, _ = gp.evaluate([(6.1, 8000), (8.6, 24000)])
    assert status == gp.OK


def test_evaluate_all_incompatible_fails():
    status, _ = gp.evaluate([(6.1, 8000), (7.0, 11000)])
    assert status == gp.FAIL


def test_evaluate_only_borderline_warns():
    status, _ = gp.evaluate([(7.5, 11_800)])
    assert status == gp.WARN


# --- main : code retour avec nvidia-smi injecté ------------------------------------------
def test_main_ok_returns_0(monkeypatch):
    monkeypatch.setattr(gp, "_query_nvidia_smi", lambda: "8.9, 24000\n")
    assert gp.main([]) == 0


def test_main_fail_returns_1(monkeypatch):
    monkeypatch.setattr(gp, "_query_nvidia_smi", lambda: "6.1, 8000\n")
    assert gp.main([]) == 1


def test_main_warn_returns_0(monkeypatch):
    monkeypatch.setattr(gp, "_query_nvidia_smi", lambda: "7.5, 11800\n")
    assert gp.main([]) == 0


def test_main_nvidia_smi_error_returns_1(monkeypatch):
    def _boom():
        raise FileNotFoundError("nvidia-smi")

    monkeypatch.setattr(gp, "_query_nvidia_smi", _boom)
    assert gp.main([]) == 1

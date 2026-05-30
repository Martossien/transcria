"""Tests du superviseur de moteurs STT — cycle de vie CAS A/B/C.

Cœur de l'autonomie VRAM du STT (docs/SERVICE_RESSOURCES_GPU.md §2.2, plan §12.3).
Dépendances injectées (sonde santé, lanceur, planificateur) → aucun GPU ni
subprocess réel. On vérifie la décision :
  - CAS A : moteur déjà sain → réutilisé, pas de lancement ;
  - CAS B : éteint + VRAM dispo → planifié puis lancé (place ou relocalisation) ;
  - CAS C : VRAM saturée → busy (503 en amont), pas de lancement.
"""
from __future__ import annotations

import pytest

from transcria.gpu.stt_engine_supervisor import (
    EngineSpec,
    SttEngineSupervisor,
    http_health_prober,
    make_script_launcher,
)
from transcria.gpu.stt_vram_planner import GpuState, SttVramPlanner

_SPEC = EngineSpec(
    name="cohere", script="scripts/launch_stt_cohere.sh",
    gpu=3, gpu_mem=0.85, port=8003, health_url="http://127.0.0.1:8003/v1/models",
)


def _planner(states):
    return SttVramPlanner(lambda: states)


class _Launcher:
    """Lanceur factice : enregistre les appels, succès configurable."""

    def __init__(self, ok=True):
        self.ok = ok
        self.calls: list[tuple[str, int]] = []

    def __call__(self, spec, gpu_index):
        self.calls.append((spec.name, gpu_index))
        return self.ok


def _supervisor(states, *, health, launcher, auto_relocate=False):
    return SttEngineSupervisor(
        planner=_planner(states),
        health_prober=lambda url: health,
        launcher=launcher,
        auto_relocate=auto_relocate,
    )


# ── CAS A ─────────────────────────────────────────────────────────────────────

def test_cas_a_already_healthy_is_reused():
    launcher = _Launcher()
    sup = _supervisor([GpuState(3, 24000, 24000)], health=True, launcher=launcher)
    r = sup.ensure_ready(_SPEC)
    assert r.status == "ready"
    assert r.gpu_index == 3
    assert launcher.calls == []          # rien lancé


# ── CAS B ─────────────────────────────────────────────────────────────────────

def test_cas_b_down_with_room_is_launched():
    launcher = _Launcher(ok=True)
    sup = _supervisor([GpuState(3, 24000, 24000)], health=False, launcher=launcher)
    r = sup.ensure_ready(_SPEC)
    assert r.status == "launched"
    assert r.gpu_index == 3
    assert launcher.calls == [("cohere", 3)]


def test_cas_b_relocates_when_assigned_full():
    launcher = _Launcher(ok=True)
    states = [GpuState(3, 4000, 24000), GpuState(5, 24000, 24000)]
    sup = _supervisor(states, health=False, launcher=launcher, auto_relocate=True)
    r = sup.ensure_ready(_SPEC)
    assert r.status == "launched"
    assert r.gpu_index == 5               # relocalisé
    assert launcher.calls == [("cohere", 5)]


def test_launch_failure_is_error():
    launcher = _Launcher(ok=False)
    sup = _supervisor([GpuState(3, 24000, 24000)], health=False, launcher=launcher)
    r = sup.ensure_ready(_SPEC)
    assert r.status == "error"
    assert launcher.calls == [("cohere", 3)]


# ── CAS C ─────────────────────────────────────────────────────────────────────

def test_cas_c_no_vram_is_busy_no_launch():
    launcher = _Launcher()
    sup = _supervisor([GpuState(3, 4000, 24000)], health=False, launcher=launcher,
                      auto_relocate=False)
    r = sup.ensure_ready(_SPEC)
    assert r.status == "busy"
    assert r.gpu_index is None
    assert launcher.calls == []          # CAS C : on ne lance rien


def test_cas_c_when_relocate_finds_nothing():
    launcher = _Launcher()
    states = [GpuState(3, 4000, 24000), GpuState(5, 5000, 24000)]
    sup = _supervisor(states, health=False, launcher=launcher, auto_relocate=True)
    r = sup.ensure_ready(_SPEC)
    assert r.status == "busy"
    assert launcher.calls == []


# ── Adaptateur sonde HTTP ─────────────────────────────────────────────────────

class _Resp:
    def __init__(self, code):
        self.status_code = code


def test_http_health_prober_ok_and_ko():
    class _SessOK:
        def get(self, url, timeout=None):
            return _Resp(200)

    class _SessBoom:
        def get(self, url, timeout=None):
            raise RuntimeError("refused")

    assert http_health_prober("http://h/v1/models", session=_SessOK()) is True
    assert http_health_prober("http://h/v1/models", session=_SessBoom()) is False


# ── Adaptateur lanceur de script (runner/sleeper injectés) ───────────────────

def test_launcher_runs_script_with_overridden_env_and_waits_ready():
    runs: list[tuple] = []

    def runner(script, env, log_path):
        runs.append((script, env, log_path))

    # santé : False puis True (prêt au 2e poll).
    state = {"n": 0}

    def health(url):
        state["n"] += 1
        return state["n"] >= 2

    sleeps: list[float] = []
    launcher = make_script_launcher(
        health_prober=health, runner=runner, sleeper=sleeps.append, ready_timeout_s=10,
    )
    ok = launcher(_SPEC, 5)
    assert ok is True
    assert runs[0][0] == "scripts/launch_stt_cohere.sh"
    assert runs[0][1] == {"STT_GPU": "5", "STT_PORT": "8003"}   # GPU relocalisé surchargé
    assert sleeps == [2.0]                                       # un poll d'attente


def test_launcher_returns_false_on_timeout():
    runs: list[tuple] = []
    launcher = make_script_launcher(
        health_prober=lambda url: False,
        runner=lambda *a: runs.append(a),
        sleeper=lambda s: None,
        ready_timeout_s=0,         # readiness jamais atteinte
    )
    assert launcher(_SPEC, 3) is False
    assert len(runs) == 1          # le lancement a bien été tenté

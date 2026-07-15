"""Tests du registre de phases (workflow/phases/__init__.py — B1 lot 3)."""
import inspect

import pytest

from transcria.workflow import phases
from transcria.workflow.phases import (
    correction,
    diarization,
    export,
    final_review,
    multi_stt_review,
    quality,
    refine,
    summary,
    transcription,
)
from transcria.jobs.store import JobStore
from transcria.workflow.gpu_phase import GpuPhaseSession
from transcria.workflow.progress import WorkflowProgressReporter
from transcria.workflow.runner import WorkflowRunner

EXPECTED = {
    "summary": (summary.run, True),
    "transcription": (transcription.run, True),
    "diarization": (diarization.run_diarization, True),
    "multi_stt_review": (multi_stt_review.run, True),
    "correction": (correction.run, False),
    "final_review": (final_review.run, False),
    "quality": (quality.run, False),
    "refine": (refine.run, False),
    "export": (export.run, False),
}


class TestRegistryContents:
    def test_exactly_the_nine_phases(self):
        assert sorted(phases.REGISTRY) == sorted(EXPECTED)

    def test_entries_point_to_module_functions(self):
        for name, (fn, needs_audio) in EXPECTED.items():
            spec = phases.get(name)
            assert spec.run is fn
            assert spec.needs_audio is needs_audio
            assert spec.name == name

    def test_needs_audio_matches_signatures(self):
        # La convention de signature est vérifiable : audio_path présent ssi needs_audio.
        for spec in phases.REGISTRY.values():
            params = list(inspect.signature(spec.run).parameters)
            assert params[0] == "runner"
            assert ("audio_path" in params) is spec.needs_audio

    def test_get_unknown_phase_raises_with_known_names(self):
        with pytest.raises(KeyError, match="Phase inconnue.*'inexistante'"):
            phases.get("inexistante")


class TestFacadeDispatchesViaRegistry:
    """La façade doit passer par le registre — pas par des appels de module figés."""

    @pytest.mark.parametrize(
        "method,phase,args",
        [
            ("run_summary", "summary", ("job", "/tmp/a.wav", {})),
            ("run_transcription", "transcription", ("job", "/tmp/a.wav", {})),
            ("run_diarization", "diarization", ("job", "/tmp/a.wav", {})),
            ("run_multi_stt_review", "multi_stt_review", ("job", "/tmp/a.wav", {})),
            ("run_correction", "correction", ("job", {})),
            ("run_final_review", "final_review", ("job", {})),
            ("run_quality_checks", "quality", ("job", {})),
            ("run_refine", "refine", ("job", {})),
            ("build_export", "export", ("job", {})),
        ],
    )
    def test_public_method_calls_registry_entry(self, monkeypatch, method, phase, args):
        calls = []
        spec = phases.REGISTRY[phase]
        monkeypatch.setitem(
            phases.REGISTRY,
            phase,
            phases.PhaseSpec(spec.name, lambda *a: calls.append(a) or {"ok": True}, spec.needs_audio),
        )
        runner = WorkflowRunner.__new__(WorkflowRunner)  # pas d'infra : seul le dispatch est testé
        result = getattr(runner, method)(*args)
        assert result == {"ok": True}
        assert calls == [(runner, *args)]


class TestRunnerInfrastructureInjection:
    """DoD B1 : __init__ ne construit plus d'infrastructure — elle est injectable."""

    def test_injected_gpu_and_progress_are_used_as_is(self):
        gpu = object.__new__(GpuPhaseSession)
        gpu.vram = object()
        gpu.allocator = object()
        progress = object()
        runner = WorkflowRunner(JobStore, {}, gpu=gpu, progress=progress)
        assert runner.gpu is gpu
        assert runner.progress is progress
        # Les vues write-through vram/allocator suivent la session injectée.
        assert runner.vram is gpu.vram
        assert runner.allocator is gpu.allocator

    def test_defaults_build_the_historical_factories(self):
        runner = WorkflowRunner(JobStore, {})
        assert isinstance(runner.gpu, GpuPhaseSession)
        assert isinstance(runner.progress, WorkflowProgressReporter)

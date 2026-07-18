"""§4.1 — backend STT imposé par le profil (srt_moss) : câblage phase + Transcriber.

Purs : le job est une doublure (`get_extra_data`), aucun modèle chargé — le
Transcriber est construit avec la factory monkeypatchée pour capturer le backend.
"""
from __future__ import annotations

from types import SimpleNamespace

from transcria.workflow.phases.transcription import resolve_phase_backend

_CFG = {"models": {"stt_backend": "cohere"}}


def _job(profile_id: str | None = None, mode: str = "fast"):
    extra = {"execution": {"processing_profile_id": profile_id}} if profile_id else {}
    return SimpleNamespace(
        id="j1", processing_mode=mode, get_extra_data=lambda: extra,
    )


def test_profil_sans_backend_impose_suit_la_config():
    """Garde historique : tout profil sans stt_backend → models.stt_backend."""
    profile_backend, backend = resolve_phase_backend(_job("srt_locuteurs"), _CFG)
    assert profile_backend is None
    assert backend == "cohere"


def test_srt_moss_impose_moss():
    profile_backend, backend = resolve_phase_backend(_job("srt_moss"), _CFG)
    assert profile_backend == "moss"
    assert backend == "moss"


def test_job_sans_profil_mode_legacy_suit_la_config():
    profile_backend, backend = resolve_phase_backend(_job(None, mode="quality"), _CFG)
    assert profile_backend is None
    assert backend == "cohere"


def test_transcriber_respecte_le_backend_impose(monkeypatch):
    """Le Transcriber transmet le backend imposé à la factory et le journalise
    dans ses métadonnées (backend effectif, pas celui de la config)."""
    captured = {}

    def _fake_factory(config, backend=None, device=None):
        captured["backend"] = backend
        return SimpleNamespace(concurrent_safe=False, model_name=f"fake:{backend}")

    monkeypatch.setattr("transcria.stt.transcription.create_transcriber", _fake_factory)
    from transcria.stt.transcription import Transcriber

    tr = Transcriber(_CFG, gpu_index=0, backend="moss")
    assert captured["backend"] == "moss"
    assert tr.backend == "moss"

    tr_default = Transcriber(_CFG, gpu_index=0)
    assert captured["backend"] == "cohere"
    assert tr_default.backend == "cohere"


def test_enveloppe_single_pass_refuse_les_reunions_longues(tmp_path):
    """§4.1 : profil srt_moss + audio > moss.single_pass_max_s → refus AVANT GPU."""
    import json

    from transcria.workflow.phases.transcription import check_single_pass_envelope

    jdir = tmp_path / "j1" / "metadata"
    jdir.mkdir(parents=True)
    (jdir / "audio_analysis.json").write_text(json.dumps({"duration_seconds": 2760.0}))
    cfg = {"storage": {"jobs_dir": str(tmp_path)}, "moss": {"single_pass_max_s": 600}}

    err = check_single_pass_envelope(SimpleNamespace(id="j1"), cfg, "moss")
    assert err is not None and "10 min" in err and "46 min" in err

    # Sous le plafond : OK.
    (jdir / "audio_analysis.json").write_text(json.dumps({"duration_seconds": 300.0}))
    assert check_single_pass_envelope(SimpleNamespace(id="j1"), cfg, "moss") is None
    # Autres profils (backend non imposé) : jamais concernés, même config.
    assert check_single_pass_envelope(SimpleNamespace(id="j1"), cfg, None) is None

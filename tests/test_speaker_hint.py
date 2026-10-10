"""Tests de apply_speaker_hint : fourchette de locuteurs par job + guard Sortformer."""

from transcria.stt.diarizer_factory import apply_speaker_hint


def test_min_max_range_sets_bounds_without_num_speakers():
    cfg = apply_speaker_hint({}, {"min": 3, "max": 7})
    assert cfg["diarization"]["min_speakers"] == 3
    assert cfg["diarization"]["max_speakers"] == 7
    assert "num_speakers" not in cfg["diarization"]


def test_equal_min_max_sets_exact_num_speakers():
    cfg = apply_speaker_hint({}, {"min": 5, "max": 5})
    assert cfg["diarization"]["num_speakers"] == 5
    assert cfg["diarization"]["min_speakers"] == 5
    assert cfg["diarization"]["max_speakers"] == 5


def test_range_clears_preexisting_num_speakers():
    base = {"diarization": {"num_speakers": 4}}
    cfg = apply_speaker_hint(base, {"min": 2, "max": 6})
    assert "num_speakers" not in cfg["diarization"]
    # l'original n'est pas muté
    assert base["diarization"]["num_speakers"] == 4


def test_inverted_bounds_are_swapped():
    cfg = apply_speaker_hint({}, {"min": 8, "max": 3})
    assert cfg["diarization"]["min_speakers"] == 3
    assert cfg["diarization"]["max_speakers"] == 8


def test_invalid_or_empty_hint_returns_unchanged_copy():
    base = {"diarization": {"max_speakers": 20}, "models": {}}
    assert apply_speaker_hint(base, None)["diarization"]["max_speakers"] == 20
    assert apply_speaker_hint(base, {})["diarization"]["max_speakers"] == 20
    assert apply_speaker_hint(base, {"min": 0, "max": -2})["diarization"] == {"max_speakers": 20}


def test_bool_and_non_numeric_bounds_ignored():
    cfg = apply_speaker_hint({}, {"min": True, "max": "five"})
    assert cfg.get("diarization", {}) == {}


def test_sortformer_switches_to_pyannote_when_user_max_above_cap():
    base = {"models": {"diarization_backend": "sortformer"}}
    cfg = apply_speaker_hint(base, {"min": 2, "max": 7})
    assert cfg["models"]["diarization_backend"] == "pyannote"


def test_sortformer_kept_when_user_max_within_cap():
    base = {"models": {"diarization_backend": "sortformer"}}
    cfg = apply_speaker_hint(base, {"min": 2, "max": 4})
    assert cfg["models"]["diarization_backend"] == "sortformer"


def test_sortformer_switches_when_only_min_above_cap():
    base = {"models": {"diarization_backend": "sortformer"}}
    cfg = apply_speaker_hint(base, {"min": 6})
    assert cfg["models"]["diarization_backend"] == "pyannote"


def test_sortformer_not_switched_without_user_hint():
    # le maximum global par défaut ne doit pas désactiver Sortformer
    base = {"models": {"diarization_backend": "sortformer"}, "diarization": {"max_speakers": 20}}
    assert apply_speaker_hint(base, None)["models"]["diarization_backend"] == "sortformer"
    assert apply_speaker_hint(base, {})["models"]["diarization_backend"] == "sortformer"


def test_pyannote_backend_untouched_by_guard():
    base = {"models": {"diarization_backend": "pyannote"}}
    cfg = apply_speaker_hint(base, {"min": 2, "max": 9})
    assert cfg["models"]["diarization_backend"] == "pyannote"


def test_original_config_not_mutated():
    base = {"models": {"diarization_backend": "sortformer"}, "diarization": {}}
    apply_speaker_hint(base, {"min": 1, "max": 9})
    assert base["models"]["diarization_backend"] == "sortformer"
    assert base["diarization"] == {}


# ── « auto » (0.4.7) : Nemotron 3 de 1 à 8 voix, pyannote au-delà ou comptage exact ──

from transcria.stt.diarizer_factory import (  # noqa: E402 — bloc 0.4.7 ajouté en fin de module
    DIARIZATION_AUTO,
    configured_diarization_backend,
    get_diarizer_vram_mb,
    resolve_auto_backend,
    resolve_diarization_backend,
)


def _auto(**probes):
    base = {"nemotron_available": True, "pyannote_usable": True, "upper_bound": None, "exact_count": False}
    return resolve_auto_backend(**{**base, **probes})


class TestResolveAutoBackend:
    def test_sans_fourchette_nemotron(self):
        assert _auto() == resolve_auto_backend(upper_bound=None, exact_count=False,
                                               nemotron_available=True, pyannote_usable=True)
        assert _auto().backend == "nemotron_diar" and _auto().reason == "default"

    def test_fourchette_dans_la_capacite_nemotron(self):
        assert _auto(upper_bound=8).backend == "nemotron_diar"

    def test_au_dela_de_huit_pyannote(self):
        r = _auto(upper_bound=9)
        assert (r.backend, r.reason) == ("pyannote", "above_capacity")

    def test_comptage_exact_pyannote(self):
        r = _auto(upper_bound=3, exact_count=True)
        assert (r.backend, r.reason) == ("pyannote", "exact_count")

    def test_nemotron_absent_pyannote_historique(self):
        r = _auto(nemotron_available=False)
        assert (r.backend, r.reason) == ("pyannote", "nemotron_unavailable")

    def test_sans_token_nemotron_meme_au_dela(self):
        # pyannote inutilisable (gated) : Nemotron plutôt qu'un échec, même > 8 ou exact.
        assert _auto(pyannote_usable=False, upper_bound=12).backend == "nemotron_diar"
        assert _auto(pyannote_usable=False, exact_count=True, upper_bound=4).backend == "nemotron_diar"

    def test_sans_rien_pyannote(self):
        assert _auto(nemotron_available=False, pyannote_usable=False).backend == "pyannote"


class TestResolveDiarizationBackend:
    def test_valeur_explicite_inchangee(self):
        r = resolve_diarization_backend({"models": {"diarization_backend": "sortformer"}}, {"min": 20, "max": 30},
                                        nemotron_available=True, pyannote_ok=True)
        assert (r.backend, r.reason) == ("sortformer", "configured")

    def test_auto_lit_la_fourchette(self):
        cfg = {"models": {"diarization_backend": "auto"}}
        assert resolve_diarization_backend(cfg, {"min": 2, "max": 6}, nemotron_available=True, pyannote_ok=True).backend == "nemotron_diar"
        assert resolve_diarization_backend(cfg, {"min": 2, "max": 12}, nemotron_available=True, pyannote_ok=True).backend == "pyannote"
        assert resolve_diarization_backend(cfg, {"min": 5, "max": 5}, nemotron_available=True, pyannote_ok=True).backend == "pyannote"
        assert resolve_diarization_backend(cfg, {"min": 12, "max": 2}, nemotron_available=True, pyannote_ok=True).backend == "pyannote"

    def test_sondes_par_defaut_sans_binaire_pyannote(self, tmp_path, monkeypatch):
        # Aucun binaire/GGUF aux chemins donnés ⇒ Nemotron indisponible ⇒ pyannote (historique).
        monkeypatch.setenv("HF_TOKEN", "x")
        cfg = {"models": {"diarization_backend": "auto"},
               "nemotron_diar": {"cli_path": str(tmp_path / "absent"), "model_path": str(tmp_path / "absent.gguf")}}
        assert resolve_diarization_backend(cfg).backend == "pyannote"

    def test_sondes_par_defaut_avec_binaire_et_gguf(self, tmp_path, monkeypatch):
        cli = tmp_path / "audiocpp_cli"
        cli.write_text("#!/bin/sh\n")
        cli.chmod(0o755)
        (tmp_path / "m.gguf").write_bytes(b"x")
        monkeypatch.setenv("HF_TOKEN", "x")
        cfg = {"models": {"diarization_backend": "auto"},
               "nemotron_diar": {"cli_path": str(cli), "model_path": str(tmp_path / "m.gguf")}}
        assert resolve_diarization_backend(cfg).backend == "nemotron_diar"
        monkeypatch.delenv("HF_TOKEN")
        assert resolve_diarization_backend(cfg, {"min": 9, "max": 9}).reason == "pyannote_unusable"


class TestApplySpeakerHintAuto:
    _CFG = {"models": {"diarization_backend": "auto"}}

    def test_resout_meme_sans_fourchette(self):
        cfg = apply_speaker_hint(self._CFG, None, nemotron_available=True, pyannote_ok=True)
        assert cfg["models"]["diarization_backend"] == "nemotron_diar"
        assert cfg["models"]["diarization_backend_auto_reason"] == "default"

    def test_resout_avec_fourchette_et_pose_les_bornes(self):
        cfg = apply_speaker_hint(self._CFG, {"min": 2, "max": 10}, nemotron_available=True, pyannote_ok=True)
        assert cfg["models"]["diarization_backend"] == "pyannote"
        assert cfg["diarization"] == {"min_speakers": 2, "max_speakers": 10}

    def test_comptage_exact_pyannote_avec_num_speakers(self):
        cfg = apply_speaker_hint(self._CFG, {"min": 4, "max": 4}, nemotron_available=True, pyannote_ok=True)
        assert cfg["models"]["diarization_backend"] == "pyannote"
        assert cfg["diarization"]["num_speakers"] == 4

    def test_original_non_mute(self):
        base = {"models": {"diarization_backend": "auto"}}
        apply_speaker_hint(base, {"min": 1, "max": 3}, nemotron_available=True, pyannote_ok=True)
        assert base == {"models": {"diarization_backend": "auto"}}

    def test_backend_explicite_garde_le_guard_historique(self):
        cfg = apply_speaker_hint({"models": {"diarization_backend": "nemotron_diar"}}, {"min": 2, "max": 9},
                                 nemotron_available=True, pyannote_ok=True)
        assert cfg["models"]["diarization_backend"] == "pyannote"       # > 8 ⇒ guard BACKEND_MAX_SPEAKERS
        assert "diarization_backend_auto_reason" not in cfg["models"]


def test_configured_backend_normalise():
    assert configured_diarization_backend({}) == "pyannote"
    assert configured_diarization_backend({"models": {"diarization_backend": " Auto "}}) == DIARIZATION_AUTO


class TestVramAtUpload:
    _NEMO = {"models": {"diarization_backend": "nemotron_diar"}, "gpu": {"nemotron_diar_vram_mb": 2000}}

    def test_nemotron_auto_zero_a_l_upload_mais_gpu_au_pipeline(self):
        assert get_diarizer_vram_mb("nemotron_diar", self._NEMO, at_upload=True) == 0
        assert get_diarizer_vram_mb("nemotron_diar", self._NEMO) == 2000

    def test_nemotron_cuda_force_garde_le_gpu_partout(self):
        cfg = {**self._NEMO, "nemotron_diar": {"backend": "cuda"}}
        assert get_diarizer_vram_mb("nemotron_diar", cfg, at_upload=True) == 2000

    def test_pyannote_inchange(self):
        cfg = {"gpu": {"pyannote_vram_mb": 2500}}
        assert get_diarizer_vram_mb("pyannote", cfg, at_upload=True) == 2500

    def test_auto_resolu_sans_fourchette(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HF_TOKEN", "x")
        cfg = {"models": {"diarization_backend": "auto"}, "gpu": {"pyannote_vram_mb": 2500},
               "nemotron_diar": {"cli_path": str(tmp_path / "absent"), "model_path": str(tmp_path / "absent.gguf")}}
        assert get_diarizer_vram_mb("auto", cfg) == 2500      # Nemotron absent ⇒ pyannote

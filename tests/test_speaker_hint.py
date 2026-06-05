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

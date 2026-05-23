"""Tests des helpers d'affichage web."""

from transcria.web.routes import (
    _audio_diagnostic_view,
    _fill_missing_speaker_genders,
    _processing_diagnostic_view,
)


def test_audio_diagnostic_view_keeps_user_message_simple():
    view = _audio_diagnostic_view({
        "risk_level": "degrade",
        "rms": 0.006,
        "estimated_snr_db": 3.2,
        "silence_ratio": 0.4,
        "bandwidth_95_hz": 2600.0,
        "flags": ["audio_tres_faible", "snr_faible", "risque_transcription_non_fiable"],
    })

    assert view["label"] == "Son difficile"
    assert view["class"] == "danger"
    assert view["recommended_mode"] == "quality"
    assert "volume très faible" in view["reasons"]


def test_processing_diagnostic_view_counts_reliability_and_limits_segments():
    segments = [
        {
            "start": 0.0,
            "end": 1.0,
            "speaker": "SPEAKER_00",
            "text": "segment à vérifier",
            "reliability": "degrade",
            "reliability_reasons": ["audio_preflight_degrade"],
        },
        {"start": 1.0, "end": 2.0, "text": "segment ok", "reliability": "ok"},
    ]

    view = _processing_diagnostic_view(
        {"backend": "cohere", "chunking_mode": "pyannote_turns", "segments": 2},
        segments,
    )

    assert view["backend"] == "cohere"
    assert view["chunking_mode"] == "pyannote_turns"
    assert view["reliability_counts"] == {"degrade": 1, "ok": 1}
    assert len(view["suspect_segments"]) == 1
    assert view["suspect_segments"][0]["text"] == "segment à vérifier"


def test_fill_missing_speaker_genders_uses_mapping_without_overwriting_existing():
    speakers_data = {
        "speakers": [
            {"speaker_id": "SPEAKER_00", "gender": ""},
            {"speaker_id": "SPEAKER_01", "gender": "female"},
        ]
    }
    mapping_data = {
        "mapping": {
            "SPEAKER_00": {"gender": "male"},
            "SPEAKER_01": {"gender": "male"},
        }
    }

    changed = _fill_missing_speaker_genders(speakers_data, mapping_data, {}, {})

    assert changed is True
    assert speakers_data["speakers"][0]["gender"] == "male"
    assert speakers_data["speakers"][1]["gender"] == "female"


def test_fill_missing_speaker_genders_uses_audio_scene_fallback():
    speakers_data = {
        "speakers": [
            {"speaker_id": "SPEAKER_00", "gender": ""},
        ]
    }
    audio_scene = {
        "gender_segments": [
            {"start": 0.0, "end": 2.0, "label": "female"},
        ]
    }
    speaker_turns = {
        "turns": [
            {"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00"},
        ]
    }

    changed = _fill_missing_speaker_genders(speakers_data, {}, audio_scene, speaker_turns)

    assert changed is True
    assert speakers_data["speakers"][0]["gender"] == "female"

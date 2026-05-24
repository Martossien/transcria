"""Tests des helpers d'affichage web."""

from transcria.web.routes import (
    _audio_diagnostic_view,
    _enrich_lexicon_context_audio,
    _fill_missing_speaker_genders,
    _processing_diagnostic_view,
    _recover_summary_speaker_hints,
    _resolve_context_audio_range,
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


def test_recover_summary_speaker_hints_repairs_missing_llm_fields():
    class FakeFilesystem:
        def __init__(self):
            self.saved = None

        @staticmethod
        def load_text(path):
            assert path == "summary/summary.md"
            return """## Participants probables

- SPEAKER_00 [Sylvain Martin] : personne s'identifiant dans un extrait vocal (rôle non identifiable au-delà de l'auto-désignation)
"""

        def save_json(self, path, data):
            assert path == "context/meeting_context.json"
            self.saved = data

    fs = FakeFilesystem()
    recovered = _recover_summary_speaker_hints(fs, {})

    assert recovered["speaker_roles_llm"]["SPEAKER_00"]["label"] == "Sylvain Martin"
    assert "Sylvain Martin" in recovered["participants_detectes"]
    assert fs.saved == recovered


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


def test_enrich_lexicon_context_audio_marks_playable_and_counts_listened():
    lexicon = [
        {
            "term": "Emmental",
            "contexts": [
                {"timecode": "5.4s→26.4s", "quote": "extrait", "listened": True},
                {"timecode": "sans timecode", "quote": "extrait"},
            ],
        }
    ]

    enriched = _enrich_lexicon_context_audio(lexicon)

    assert enriched[0]["contexts_playable_count"] == 1
    assert enriched[0]["contexts_listened_count"] == 1
    assert enriched[0]["contexts"][0]["audio_start"] == 5.4
    assert enriched[0]["contexts"][0]["audio_end"] == 26.4
    assert enriched[0]["contexts"][1]["audio_available"] is False
    assert "audio_start" not in lexicon[0]["contexts"][0]


def test_enrich_lexicon_context_audio_repairs_timecode_inside_quote():
    lexicon = [
        {
            "term": "Emmental",
            "contexts": [
                {"timecode": "", "quote": "00:05] SPEAKER_XX: « De l'émenteal, ça ira comme ça ? »"},
            ],
        }
    ]

    enriched = _enrich_lexicon_context_audio(lexicon)
    context = enriched[0]["contexts"][0]

    assert context["timecode"] == "00:05"
    assert context["speaker"] == "SPEAKER_XX"
    assert context["quote"] == "De l'émenteal, ça ira comme ça ?"
    assert context["audio_available"] is True


def test_resolve_context_audio_range_reanchors_mismatched_llm_timecode():
    segments = [
        {
            "start": 5.4,
            "end": 26.4,
            "text": "Fait pas chaud ce matin. Mettez-moi un peu d'émental. De l'émenteal, ça ira comme ça ? Oui.",
        },
        {"start": 27.0, "end": 30.6, "text": "Le mieux, c'est d'y goûter."},
    ]

    resolved = _resolve_context_audio_range("27.0s→30.6s", "De l'émenteal, ça ira comme ça ?", segments)

    assert resolved[0] > 5.4
    assert resolved[1] < 26.4
    assert resolved[2] is True


def test_resolve_context_audio_range_keeps_matching_timecode():
    segments = [
        {"start": 27.0, "end": 30.6, "text": "Le mieux, c'est d'y goûter."},
    ]

    resolved = _resolve_context_audio_range("27.0s→30.6s", "Le mieux, c'est d'y goûter.", segments)

    assert resolved == (27.0, 30.6, False)


def test_resolve_context_audio_range_estimates_quote_position_for_single_bad_timestamp():
    segments = [
        {
            "start": 5.4,
            "end": 26.4,
            "text": "Fait pas chaud ce matin. Qu'est-ce qu'il vous faudra ? Mettez-moi un peu d'émental, s'il-vous-plaît. De l'émenteal, ça ira comme ça ? Oui.",
        },
    ]

    resolved = _resolve_context_audio_range("00:00:05", "Mettez-moi un peu d'émental", segments)

    assert resolved[0] > 10.0
    assert resolved[1] < 22.0
    assert resolved[2] is True


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

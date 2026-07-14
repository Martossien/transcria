"""Tests des helpers d'affichage web."""

from transcria.web.lexicon_views import (
    enrich_lexicon_context_audio as _enrich_lexicon_context_audio,
    resolve_context_audio_range as _resolve_context_audio_range,
)
from transcria.web.pages_routes import (
    _audio_diagnostic_view,
    _build_difficulty_frise,
    _fill_missing_speaker_genders,
    _processing_diagnostic_view,
    _recover_summary_speaker_hints,
)
from transcria.web.wizard_api import _normalize_speaker_hint


def test_normalize_speaker_hint_keeps_valid_range():
    assert _normalize_speaker_hint({"min": 3, "max": 7}) == {"min": 3, "max": 7}


def test_normalize_speaker_hint_swaps_inverted_bounds():
    assert _normalize_speaker_hint({"min": 9, "max": 2}) == {"min": 2, "max": 9}


def test_normalize_speaker_hint_rejects_out_of_range_and_blanks():
    assert _normalize_speaker_hint({"min": "", "max": 99}) == {"min": None, "max": None}
    assert _normalize_speaker_hint({}) == {"min": None, "max": None}


def test_normalize_speaker_hint_accepts_numeric_strings():
    assert _normalize_speaker_hint({"min": "4", "max": "4"}) == {"min": 4, "max": 4}


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


def test_audio_diagnostic_view_surfaces_squim_dnsmos_and_difficulty():
    view = _audio_diagnostic_view({
        "risk_level": "degrade",
        "flags": ["squim_stoi_faible", "dnsmos_ovrl_faible"],
        "squim_global": {"stoi": 0.62, "pesq": 2.1, "sisdr": 3.4},
        "dnsmos_global": {"sig": 3.0, "bak": 2.1, "ovrl": 2.3},
        "difficulty_summary": {"windows": 40, "degrade": 3, "suspect": 5, "ok": 32},
    })

    assert view["perceptual"]["squim"] == {"stoi": 0.62, "pesq": 2.1, "sisdr": 3.4}
    assert view["perceptual"]["dnsmos"] == {"sig": 3.0, "bak": 2.1, "ovrl": 2.3}
    assert view["difficulty"] == {"windows": 40, "degrade": 3, "suspect": 5}
    assert "intelligibilité réduite" in view["reasons"]
    # BAK (2.1) < SIG (3.0) → bruit dominant.
    assert view["advice"]["class"] == "info"
    assert "Bruit de fond dominant" in view["advice"]["text"]


def test_audio_diagnostic_view_degraded_speech_advice():
    view = _audio_diagnostic_view({
        "risk_level": "suspect",
        "flags": ["squim_pesq_faible"],
        "dnsmos_global": {"sig": 2.0, "bak": 3.5, "ovrl": 2.4},
    })
    # SIG (2.0) < BAK (3.5) → parole dégradée.
    assert view["advice"]["class"] == "warning"
    assert "Parole elle-même dégradée" in view["advice"]["text"]


def test_build_difficulty_frise_empty_returns_empty_list():
    assert _build_difficulty_frise([]) == []
    assert _build_difficulty_frise(None) == []


def test_build_difficulty_frise_one_segment_per_window_when_small():
    frise = _build_difficulty_frise([
        {"start": 0.0, "end": 5.0, "difficulty": "ok", "signals": []},
        {"start": 5.0, "end": 10.0, "difficulty": "suspect", "signals": ["squim_stoi_faible"]},
        {"start": 10.0, "end": 15.0, "difficulty": "degrade", "signals": ["overlap"]},
    ])
    assert [s["level"] for s in frise] == ["ok", "suspect", "degrade"]
    # Largeurs proportionnelles à la durée → ~33% chacune, somme ≈ 100.
    assert abs(sum(s["pct"] for s in frise) - 100.0) < 0.01
    assert frise[1]["label"] == "0:05–0:10"
    assert frise[2]["label"] == "0:10–0:15"


def test_build_difficulty_frise_downsamples_and_keeps_worst_level():
    windows = [
        {"start": float(i), "end": float(i + 1),
         "difficulty": "degrade" if i == 7 else "ok", "signals": ["overlap"] if i == 7 else []}
        for i in range(20)
    ]
    frise = _build_difficulty_frise(windows, max_buckets=4)
    assert len(frise) == 4
    # Le segment couvrant la fenêtre 7 hérite du pire niveau (degrade).
    degraded = [s for s in frise if s["level"] == "degrade"]
    assert len(degraded) == 1
    assert degraded[0]["start"] <= 7.0 < degraded[0]["end"]


def test_build_difficulty_frise_skips_windows_missing_bounds():
    frise = _build_difficulty_frise([
        {"difficulty": "degrade", "signals": []},
        {"start": 0.0, "end": 4.0, "difficulty": "suspect", "signals": []},
    ])
    assert len(frise) == 1
    assert frise[0]["level"] == "suspect"


def test_audio_diagnostic_view_exposes_frise_from_difficulty_map():
    view = _audio_diagnostic_view({
        "risk_level": "suspect",
        "flags": ["squim_stoi_faible"],
        "difficulty_summary": {"windows": 2, "degrade": 0, "suspect": 1, "ok": 1},
        "difficulty_map": [
            {"start": 0.0, "end": 5.0, "difficulty": "ok", "signals": []},
            {"start": 5.0, "end": 10.0, "difficulty": "suspect", "signals": ["squim_stoi_faible"]},
        ],
    })
    assert view["frise"] is not None
    assert len(view["frise"]) == 2
    # Les signaux techniques sont traduits en libellés lisibles pour le tooltip.
    assert "intelligibilité réduite" in view["frise"][1]["reasons"]


def test_audio_diagnostic_view_clean_audio_has_no_advice_or_difficulty():
    view = _audio_diagnostic_view({
        "risk_level": "ok",
        "flags": [],
        "squim_global": {"stoi": 0.95, "pesq": 4.0, "sisdr": 20.0},
        "dnsmos_global": {"sig": 4.0, "bak": 4.2, "ovrl": 4.1},
    })
    assert view["advice"] is None              # pas de conseil sur audio « ok »
    assert view["difficulty"] is None          # pas de difficulty_map (lazy)
    assert view["frise"] is None               # pas de frise sans difficulty_map
    assert view["perceptual"]["squim"]["stoi"] == 0.95   # scores quand même exposés


def test_recover_summary_speaker_hints_repairs_missing_llm_fields():
    class FakeFilesystem:
        def __init__(self):
            self.saved = None

        @staticmethod
        def load_text(path):
            assert path == "summary/summary.md"
            return """## Participants probables

- SPEAKER_00 [Alex Dupont] : personne s'identifiant dans un extrait vocal (rôle non identifiable au-delà de l'auto-désignation)
"""

        def save_json(self, path, data):
            assert path == "context/meeting_context.json"
            self.saved = data

    fs = FakeFilesystem()
    recovered = _recover_summary_speaker_hints(fs, {})

    assert recovered["speaker_roles_llm"]["SPEAKER_00"]["label"] == "Alex Dupont"
    assert "Alex Dupont" in recovered["participants_detectes"]
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


def test_enrich_lexicon_context_audio_strips_llm_quote_wrappers():
    lexicon = [
        {
            "term": "Emmental",
            "contexts": [
                {"timecode": '"[00:05]"', "quote": '"De l\'émenteal, ça ira comme ça ?"'},
                {"timecode": "", "quote": '"[00:07] SPEAKER_XX: "Le mieux, c\'est d\'y goûter.""'},
            ],
        }
    ]

    enriched = _enrich_lexicon_context_audio(lexicon)
    first = enriched[0]["contexts"][0]
    second = enriched[0]["contexts"][1]

    assert first["timecode"] == "00:05"
    assert first["quote"] == "De l'émenteal, ça ira comme ça ?"
    assert first["audio_available"] is True
    assert second["timecode"] == "00:07"
    assert second["speaker"] == "SPEAKER_XX"
    assert second["quote"] == "Le mieux, c'est d'y goûter."
    assert second["audio_available"] is True


def test_enrich_lexicon_context_audio_estimates_quote_without_timecode():
    lexicon = [
        {
            "term": "SIGLE_A",
            "contexts": [
                {"timecode": "", "quote": "SIGLE_A et SIGLE_B"},
            ],
        }
    ]
    segments = [
        {
            "start": 40.0,
            "end": 52.0,
            "text": "Nous devons contrôler SIGLE_A et SIGLE_B avant la prochaine étape.",
        }
    ]

    enriched = _enrich_lexicon_context_audio(lexicon, segments)
    context = enriched[0]["contexts"][0]

    assert context["audio_available"] is True
    assert context["audio_estimated_from_quote"] is True
    assert context["audio_start"] > 40.0
    assert context["audio_end"] <= 52.0


def test_enrich_lexicon_context_audio_keeps_unknown_quote_unavailable():
    lexicon = [
        {
            "term": "SIGLE_A",
            "contexts": [
                {"timecode": "", "quote": "SIGLE_A et SIGLE_B"},
            ],
        }
    ]
    segments = [{"start": 40.0, "end": 52.0, "text": "Un autre passage sans ces termes."}]

    enriched = _enrich_lexicon_context_audio(lexicon, segments)
    context = enriched[0]["contexts"][0]

    assert context["audio_available"] is False
    assert "audio_estimated_from_quote" not in context


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
            "text": (
                "Fait pas chaud ce matin. Qu'est-ce qu'il vous faudra ? "
                "Mettez-moi un peu d'émental, s'il-vous-plaît. "
                "De l'émenteal, ça ira comme ça ? Oui."
            ),
        },
    ]

    resolved = _resolve_context_audio_range("00:00:05", "Mettez-moi un peu d'émental", segments)

    assert resolved[0] > 10.0
    assert resolved[1] < 22.0
    assert resolved[2] is True


def test_enrich_lexicon_context_audio_handles_empty_contexts_list():
    """Un terme avec contexts=[] ne doit pas planter et doit avoir les compteurs à 0."""
    lexicon = [{"term": "Terme", "contexts": []}]
    enriched = _enrich_lexicon_context_audio(lexicon)
    assert enriched[0]["contexts_listened_count"] == 0
    assert enriched[0]["contexts_playable_count"] == 0


def test_enrich_lexicon_context_audio_handles_missing_contexts_key():
    """Un terme sans clé 'contexts' ne doit pas planter."""
    lexicon = [{"term": "Terme"}]
    enriched = _enrich_lexicon_context_audio(lexicon)
    assert enriched[0].get("contexts") is None
    assert "contexts_listened_count" not in enriched[0]


def test_enrich_lexicon_context_audio_does_not_mutate_input():
    """La liste originale ne doit pas être modifiée (deepcopy attendu)."""
    lexicon = [
        {"term": "Terme", "contexts": [{"timecode": "5.4s→26.4s", "quote": "extrait"}]}
    ]
    _enrich_lexicon_context_audio(lexicon)
    assert "audio_available" not in lexicon[0]["contexts"][0]


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

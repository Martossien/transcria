"""Tests du multi-STT ciblé (logique pure) — sélection, arbitrage, application."""
from transcria.workflow.multi_stt_review import (
    apply_secondary_texts,
    build_arbitration_messages,
    parse_arbitration_choice,
    select_review_segments,
    texts_equivalent,
)


def _dmap():
    return [
        {"start": 0.0, "end": 9.0, "difficulty": "ok", "signals": []},
        {"start": 9.0, "end": 18.0, "difficulty": "degrade", "signals": ["snr_faible"]},
        {"start": 18.0, "end": 27.0, "difficulty": "suspect", "signals": ["dnsmos_ovrl_faible"]},
    ]


def _segments():
    return [
        {"start": 0.5, "end": 4.0, "text": "segment propre"},
        {"start": 10.0, "end": 14.0, "text": "segment dégradé"},
        {"start": 19.0, "end": 23.0, "text": "segment suspect"},
        {"start": 12.0, "end": 12.4, "text": "trop court"},
        {"start": 11.0, "end": 15.0, "text": ""},
    ]


class TestSelectReviewSegments:
    def test_selectionne_uniquement_les_niveaux_demandes(self):
        out = select_review_segments(_segments(), _dmap(), levels=("degrade",))
        assert [c["index"] for c in out] == [1]
        assert out[0]["difficulty"] == "degrade"
        assert out[0]["signals"] == ["snr_faible"]

    def test_niveau_suspect_inclus_sur_demande(self):
        out = select_review_segments(_segments(), _dmap(), levels=("degrade", "suspect"))
        # Tri : sévérité d'abord (degrade avant suspect).
        assert [c["index"] for c in out] == [1, 2]

    def test_ignore_segments_courts_et_vides(self):
        indices = [c["index"] for c in select_review_segments(
            _segments(), _dmap(), levels=("degrade", "suspect"), min_duration_s=0.8
        )]
        assert 3 not in indices  # 0.4 s < min
        assert 4 not in indices  # texte vide

    def test_plafond_max_segments(self):
        out = select_review_segments(
            _segments(), _dmap(), levels=("degrade", "suspect"), max_segments=1
        )
        assert len(out) == 1
        assert out[0]["difficulty"] == "degrade"

    def test_map_vide_ou_segments_vides(self):
        assert select_review_segments([], _dmap()) == []
        assert select_review_segments(_segments(), []) == []
        assert select_review_segments(_segments(), None) == []


class TestTextsEquivalent:
    def test_equivalents_apres_normalisation(self):
        assert texts_equivalent("Le Comité, s'est réuni !", "le comite s est reuni")

    def test_differents(self):
        assert not texts_equivalent("le quorum est atteint", "le forum est atteint")


class TestArbitration:
    def test_messages_sans_exemple_reel_dans_le_systeme(self):
        messages = build_arbitration_messages(primary_text="X", secondary_text="Y")
        assert messages[0]["role"] == "system"
        # Le prompt système reste abstrait : les candidats ne vivent que côté user.
        assert "X" not in messages[0]["content"]
        assert "Transcription A" in messages[1]["content"]
        assert "Transcription B" in messages[1]["content"]

    def test_parse_choix_simple(self):
        assert parse_arbitration_choice("A") == "A"
        assert parse_arbitration_choice("B") == "B"
        assert parse_arbitration_choice(" Réponse : B ") == "B"
        assert parse_arbitration_choice("Transcription A") == "A"

    def test_parse_ignore_thinking_et_minuscules(self):
        assert parse_arbitration_choice("<think>la B semble mieux</think>A") == "A"
        # « a » minuscule (verbe français) n'est pas un choix.
        assert parse_arbitration_choice("elle a raison") is None

    def test_parse_reponse_vide_ou_illisible(self):
        assert parse_arbitration_choice("") is None
        assert parse_arbitration_choice("je ne sais pas") is None


class TestApplySecondaryTexts:
    def test_remplace_uniquement_les_choix_b(self):
        segments = _segments()
        decisions = [
            {"index": 1, "choice": "B", "secondary_text": "segment corrigé", "secondary_backend": "whisper"},
            {"index": 2, "choice": "A", "secondary_text": "ignoré", "secondary_backend": "whisper"},
        ]
        replaced = apply_secondary_texts(segments, decisions)
        assert replaced == 1
        assert segments[1]["text"] == "segment corrigé"
        assert segments[1]["multi_stt"]["original_text"] == "segment dégradé"
        assert segments[1]["multi_stt"]["secondary_backend"] == "whisper"
        assert segments[2]["text"] == "segment suspect"
        assert "multi_stt" not in segments[2]

    def test_index_invalide_ou_texte_vide_ignores(self):
        segments = _segments()
        decisions = [
            {"index": 99, "choice": "B", "secondary_text": "x", "secondary_backend": "whisper"},
            {"index": 1, "choice": "B", "secondary_text": "  ", "secondary_backend": "whisper"},
        ]
        assert apply_secondary_texts(segments, decisions) == 0
        assert segments[1]["text"] == "segment dégradé"


# ---------------------------------------------------------------------------
# Gating de l'étape dans le pipeline
# ---------------------------------------------------------------------------


class TestMultiSttStepGating:
    """L'étape multi_stt_review n'est insérée que si : flag config explicite,
    profil avec correction LLM, arbitrage LLM non désactivé."""

    def _steps(self, profile_id, *, multi_stt_enabled, arbitration_enabled=True):
        from unittest.mock import MagicMock

        from transcria.services.pipeline_service import PipelineService
        from transcria.workflow.profiles import get_profile

        svc = PipelineService.__new__(PipelineService)
        svc.config = {
            "workflow": {
                "enable_quality_mode": True,
                "arbitration_llm": {"enabled": arbitration_enabled},
                "multi_stt": {"enabled": multi_stt_enabled},
            }
        }
        svc.runner = MagicMock()
        job = MagicMock()
        job.id = "job-multi-stt"
        steps = svc._define_pipeline_steps_for_profile(job, "/audio.wav", get_profile(profile_id))
        return [s["name"] for s in steps]

    def test_desactive_par_defaut(self):
        assert "multi_stt_review" not in self._steps("word_corrige", multi_stt_enabled=False)

    def test_active_sur_profil_avec_correction(self):
        names = self._steps("word_corrige", multi_stt_enabled=True)
        assert "multi_stt_review" in names
        # Avant tout consommateur du SRT (diarisation/correction).
        assert names.index("multi_stt_review") < names.index("correction")

    def test_jamais_sur_profil_express_sans_llm(self):
        assert "multi_stt_review" not in self._steps("srt_express", multi_stt_enabled=True)

    def test_jamais_si_arbitrage_desactive(self):
        assert "multi_stt_review" not in self._steps(
            "word_corrige", multi_stt_enabled=True, arbitration_enabled=False
        )

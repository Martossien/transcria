import json
import tempfile
from pathlib import Path

import pytest

from transcria.context.meeting_context import MeetingContextManager, MEETING_TYPES
from transcria.context.participants import ParticipantsManager
from transcria.context.lexicon import LexiconManager, LEXICON_CATEGORIES, LEXICON_PRIORITIES
from transcria.context.job_context_builder import JobContextBuilder
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job, JobState


def _fake_job(job_id="j1", owner="u1"):
    return Job(id=job_id, owner_id=owner, title="Test Meeting", state=JobState.CREATED.value)


class TestMeetingContext:
    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    def test_default_context(self):
        ctx = MeetingContextManager.default_context()
        assert ctx["language"] == "fr"
        assert ctx["meeting_type"] == "Réunion interne"
        assert ctx["sensitivity"] == "normal"
        assert "title" in ctx

    def test_save_and_get(self, tmp_dir):
        job = _fake_job()
        data = {"title": "Réunion projet X", "language": "en", "sensitivity": "high"}
        saved = MeetingContextManager.save(job, tmp_dir, data)
        assert saved["title"] == "Réunion projet X"
        assert saved["language"] == "en"
        assert saved["meeting_type"] == "Réunion interne"

        loaded = MeetingContextManager.get(job, tmp_dir)
        assert loaded["title"] == "Réunion projet X"

    def test_get_returns_default_when_no_file(self, tmp_dir):
        job = _fake_job()
        ctx = MeetingContextManager.get(job, tmp_dir)
        assert ctx["language"] == "fr"

    def test_auto_suggest(self, tmp_dir):
        job = _fake_job()
        suggestions = MeetingContextManager.auto_suggest(job, tmp_dir)
        assert "title_suggere" in suggestions
        assert "type_suggere" in suggestions

    def test_save_preserves_llm_roles_and_suspect_terms(self, tmp_dir):
        job = _fake_job()
        fs = JobFilesystem(tmp_dir, job.id)
        fs.save_json("context/meeting_context.json", {
            "summary_llm": "Résumé",
            "speaker_roles_llm": {
                "SPEAKER_00": {"label": "Alice", "role": "Rôle A"},
            },
            "termes_suspects": [
                {"term": "API Gateway", "category": "technique", "priority": "critique"},
            ],
            "termes_suspects_parse_status": "extracted",
            "termes_suspects_parse_warning": "",
        })

        saved = MeetingContextManager.save(job, tmp_dir, {"title": "Titre utilisateur"})

        assert saved["title"] == "Titre utilisateur"
        assert saved["speaker_roles_llm"]["SPEAKER_00"]["role"] == "Rôle A"
        assert saved["termes_suspects"][0]["term"] == "API Gateway"
        assert saved["termes_suspects_parse_status"] == "extracted"
        assert saved["termes_suspects_parse_warning"] == ""

    def test_meeting_types_list(self):
        assert "Réunion interne" in MEETING_TYPES
        assert "Formation" in MEETING_TYPES
        assert "Autre" in MEETING_TYPES


class TestParticipants:
    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    def test_save_and_get(self, tmp_dir):
        job = _fake_job()
        participants = [
            {"name": "Alice", "function": "Manager", "is_animator": True},
            {"name": "Bob", "function": "Dev", "expected": True},
        ]
        saved = ParticipantsManager.save(job, tmp_dir, participants)
        assert len(saved) == 2
        assert saved[0]["name"] == "Alice"
        assert saved[0]["is_animator"] is True
        assert "id" in saved[0]

        loaded = ParticipantsManager.get(job, tmp_dir)
        assert len(loaded) == 2
        assert loaded[1]["name"] == "Bob"

    def test_get_empty_list_when_no_file(self, tmp_dir):
        job = _fake_job()
        assert ParticipantsManager.get(job, tmp_dir) == []

    def test_strips_whitespace(self, tmp_dir):
        job = _fake_job()
        saved = ParticipantsManager.save(job, tmp_dir, [{"name": "  Alice  ", "function": "  Dev  "}])
        assert saved[0]["name"] == "Alice"
        assert saved[0]["function"] == "Dev"

    def test_default_participant(self):
        p = ParticipantsManager.default_participant()
        assert p["name"] == ""
        assert p["expected"] is True
        assert p["is_animator"] is False


class TestLexicon:
    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    def test_save_and_get(self, tmp_dir):
        job = _fake_job()
        terms = [
            {"term": "API", "category": "technique", "priority": "critique"},
            {"term": "Dr Smith", "category": "personne", "priority": "importante"},
        ]
        saved = LexiconManager.save(job, tmp_dir, terms)
        assert len(saved) == 2
        assert saved[0]["term"] == "API"
        assert saved[0]["category"] == "technique"

        loaded = LexiconManager.get(job, tmp_dir)
        assert len(loaded) == 2

    def test_save_variants_from_string_and_comment(self, tmp_dir):
        job = _fake_job()
        saved = LexiconManager.save(job, tmp_dir, [{
            "term": "SIGLE_REF",
            "category": "sigle / métier",
            "priority": "critique",
            "variants": "SIGLE_ERR, forme développée du sigle",
            "comment": "Une variante semble une erreur STT.",
        }])
        assert saved[0]["variants"] == ["SIGLE_ERR", "forme développée du sigle"]
        assert saved[0]["comment"] == "Une variante semble une erreur STT."

    def test_save_variants_ignores_empty_markers_and_term_itself(self, tmp_dir):
        job = _fake_job()
        saved = LexiconManager.save(job, tmp_dir, [{
            "term": "Forme validée",
            "category": "organisation",
            "priority": "normale",
            "variants": ["(aucune)", "forme validée", "Graphie suspecte", "graphie suspecte"],
        }])
        assert saved[0]["variants"] == ["Graphie suspecte"]

    def test_save_contexts(self, tmp_dir):
        job = _fake_job()
        saved = LexiconManager.save(job, tmp_dir, [{
            "term": "Terme validé",
            "category": "mot suspect",
            "priority": "normale",
            "contexts": [{
                "variant": "Terme suspect",
                "timecode": "00:01:02",
                "speaker": "SPEAKER_00",
                "quote": "Un extrait contenant Terme suspect.",
                "reason": "Contexte utile.",
            }],
        }])
        assert saved[0]["contexts"][0]["quote"] == "Un extrait contenant Terme suspect."
        assert saved[0]["contexts"][0]["timecode"] == "00:01:02"

    def test_get_empty_list_when_no_file(self, tmp_dir):
        job = _fake_job()
        assert LexiconManager.get(job, tmp_dir) == []

    def test_save_preserves_context_listened_flag(self, tmp_dir):
        job = _fake_job()
        saved = LexiconManager.save(
            job,
            tmp_dir,
            [
                {
                    "term": "Emmental",
                    "contexts": [
                        {
                            "timecode": "5.4s→26.4s",
                            "speaker": "SPEAKER_00",
                            "quote": "Mettez-moi un peu d'émental",
                            "listened": True,
                        }
                    ],
                }
            ],
        )

        assert saved[0]["contexts"][0]["listened"] is True

    def test_import_from_csv(self, tmp_dir):
        job = _fake_job()
        content = "TERM1, technique, critique\nTERM2, personne, normale"
        terms = LexiconManager.import_from_file(job, tmp_dir, content)
        assert len(terms) == 2
        assert terms[0]["term"] == "TERM1"
        assert terms[0]["category"] == "technique"
        assert terms[0]["priority"] == "critique"

    def test_import_simple_list(self, tmp_dir):
        job = _fake_job()
        content = "TERM1\nTERM2\n# comment\nTERM3"
        terms = LexiconManager.import_from_file(job, tmp_dir, content)
        assert len(terms) == 3

    def test_categories_and_priorities(self):
        assert "personne" in LEXICON_CATEGORIES
        assert "organisation" in LEXICON_CATEGORIES
        assert "technique" in LEXICON_CATEGORIES
        assert "médical" in LEXICON_CATEGORIES
        assert "mot suspect" in LEXICON_CATEGORIES
        assert "critique" in LEXICON_PRIORITIES
        assert "normale" in LEXICON_PRIORITIES


class TestJobContextBuilder:
    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    def test_build_generates_yaml_and_json(self, tmp_dir):
        job = _fake_job("j-ctx-1")
        MeetingContextManager.save(job, tmp_dir, {"title": "Test", "language": "fr"})
        ParticipantsManager.save(job, tmp_dir, [{"name": "Alice", "function": "Dev"}])
        LexiconManager.save(job, tmp_dir, [{
            "term": "API",
            "category": "technique",
            "variants": ["A P I"],
            "replace_by": "API",
            "comment": "Sigle technique.",
            "contexts": [{"timecode": "00:01:02", "quote": "Contexte API."}],
        }])

        result = JobContextBuilder.build(job, tmp_dir)
        assert result["job_id"] == "j-ctx-1"
        assert result["owner_user_id"] == "u1"
        assert result["meeting"]["title"] == "Test"
        assert len(result["participants"]) == 1
        assert len(result["lexicon"]) == 1
        assert result["lexicon"][0]["replace_by"] == "API"
        assert result["lexicon"][0]["comment"] == "Sigle technique."
        assert result["lexicon"][0]["contexts"][0]["quote"] == "Contexte API."
        assert result["processing"]["default_stt_model"] == "cohere-transcribe-03-2026"

    def test_build_writes_files(self, tmp_dir):
        import os
        job = _fake_job("j-ctx-2")
        JobContextBuilder.build(job, tmp_dir)
        assert os.path.isfile(f"{tmp_dir}/j-ctx-2/context/job_context.yaml")
        assert os.path.isfile(f"{tmp_dir}/j-ctx-2/context/job_context.json")

    def test_build_adds_quality_hints_without_authority(self, tmp_dir):
        job = _fake_job("j-ctx-quality")
        fs = JobFilesystem(tmp_dir, job.id)
        fs.save_json("metadata/audio_preflight.json", {
            "risk_level": "degrade",
            "rms": 0.006,
            "estimated_snr_db": 3.2,
            "silence_ratio": 0.42,
            "bandwidth_95_hz": 2600.0,
            "flags": ["audio_tres_faible", "snr_faible", "risque_transcription_non_fiable"],
        })
        fs.save_json("metadata/audio_scene.json", {
            "has_music": True,
            "has_noise": False,
            "speech_ratio": 0.72,
            "music_ratio": 0.18,
            "problem_segments": [
                {"label": "music", "start": 12.0, "end": 20.0, "duration_s": 8.0},
            ],
        })
        fs.save_json("metadata/transcription_segments.json", [
            {
                "start": 1.0,
                "end": 2.0,
                "speaker": "SPEAKER_00",
                "text": "passage peu clair",
                "reliability": "degrade",
                "reliability_reasons": ["audio_preflight_degrade", "no_speech_prob_eleve"],
                "no_speech_prob": 0.82,
            },
            {"start": 2.1, "end": 4.0, "text": "segment fiable", "reliability": "ok"},
        ])

        result = JobContextBuilder.build(job, tmp_dir)

        hints = result["quality_hints"]
        assert "Indices de prudence uniquement" in hints["usage"]
        assert hints["audio"]["level"] == "degrade"
        assert "volume très faible" in hints["audio"]["reasons"]
        assert hints["audio"]["scene"]["has_music"] is True
        assert len(hints["segments"]) == 1
        assert hints["segments"][0]["segment_index"] == 1
        assert hints["segments"][0]["level"] == "degrade"

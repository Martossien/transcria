"""Tests directs du service pur workflow/speaker_projection.py (vague B1, étape 2).

Le comportement d'ensemble reste verrouillé par les tests historiques du runner
(qui traversent les wrappers E/S) — ici on teste le CONTRAT du service : entrées =
structures chargées, sorties = objets/contenus, zéro effet de bord fichier.
"""
from transcria.workflow import speaker_projection as sp


class TestMergeLlmSuggestions:
    def test_projette_les_champs_et_liste_les_manquants(self):
        ctx = {}
        parsed = {
            "summary_text": "# Résumé de contrôle\n\nBudget validé.",
            "language": "fr",
            "title_suggere": "Comité budget",
            "speaker_count": 3,
            "termes_suspects": ["OPEX"],
            "termes_suspects_parse_status": "ok",
            "structured_data": {"decisions": ["valider"]},
            "structured_data_parse_status": "ok",
            "speaker_roles": {"SPEAKER_00": {"label": "Anna", "role": "animatrice"}},
        }
        empty = sp.merge_llm_suggestions(ctx, parsed)

        assert ctx["title_suggere"] == "Comité budget"
        assert ctx["language"] == "fr"
        assert ctx["speaker_count_llm"] == 3
        assert ctx["termes_suspects"] == ["OPEX"]
        assert ctx["structured_data"] == {"decisions": ["valider"]}
        assert ctx["speaker_roles_llm"] == {"SPEAKER_00": {"label": "Anna", "role": "animatrice"}}
        assert ctx["summary_llm"] == parsed["summary_text"]
        # Champs non renseignés par la LLM → signalés à l'appelant (qui journalise)
        assert set(empty) == {"type_suggere", "sujet_suggere", "objectif_suggere",
                              "notes_suggeres", "participants_detectes"}

    def test_ne_remplace_pas_une_langue_deja_choisie(self):
        ctx = {"language": "en"}
        sp.merge_llm_suggestions(ctx, {"language": "fr", "summary_text": "x"})
        assert ctx["language"] == "en"

    def test_warning_de_parse_retire_quand_absent(self):
        ctx = {"termes_suspects_parse_warning": "ancien", "structured_data_parse_warning": "ancien"}
        sp.merge_llm_suggestions(ctx, {"summary_text": "x"})
        assert "termes_suspects_parse_warning" not in ctx
        assert "structured_data_parse_warning" not in ctx


class TestRenderSummaryMarkdown:
    def test_avec_extrait_et_langue(self):
        md_fr = sp.render_summary_markdown("# Résumé", "Bonjour", "fr")
        md_en = sp.render_summary_markdown("# Résumé", "Hello", "en")
        assert "## Extrait de transcription" in md_fr and md_fr.startswith("# Résumé")
        assert "## Transcript excerpt" in md_en

    def test_sans_extrait(self):
        assert sp.render_summary_markdown("# Résumé", "", "fr") == "# Résumé\n"


class TestApplySpeakerRoles:
    def test_cree_une_entree_minimale_si_participant_inconnu(self):
        proj = sp.apply_speaker_roles(
            {"SPEAKER_00": {"label": "Anna", "role": "animatrice"}},
            participants=[],
            mapping_data={},
            speaker_stats_data={},
        )
        assert proj.created == 1 and proj.updated == 0
        assert proj.participants[0]["name"] == "Anna"
        assert proj.participants[0]["role"] == "animatrice"
        assert proj.participants[0]["id"] == "speaker00"

    def test_remplit_le_role_vide_sans_ecraser_un_nom_valide(self):
        participants = [{"id": "p1", "name": "Anna Martin", "role": ""}]
        mapping_data = {"mapping": {"SPEAKER_00": {"participant_id": "p1", "name": "Anna Martin"}}}
        proj = sp.apply_speaker_roles(
            {"SPEAKER_00": {"label": "Anna", "role": "animatrice"}},
            participants, mapping_data, {},
        )
        assert proj.updated == 1
        assert proj.participants[0]["name"] == "Anna Martin"  # nom utilisateur conservé
        assert proj.participants[0]["role"] == "animatrice"

    def test_propage_le_label_dans_stats_et_mapping_sans_ecraser(self):
        stats = {"speakers": [
            {"speaker_id": "SPEAKER_00", "mapped_name": ""},
            {"speaker_id": "SPEAKER_01", "mapped_name": "Nom Validé"},
        ]}
        mapping_data = {
            "mapping": {"SPEAKER_00": {"name": "SPEAKER_00"}},
            "speakers": [{"speaker_id": "SPEAKER_00", "mapped_name": ""}],
        }
        proj = sp.apply_speaker_roles(
            {
                "SPEAKER_00": {"label": "Anna", "role": "animatrice"},
                "SPEAKER_01": {"label": "Bob", "role": "invité"},
            },
            [], mapping_data, stats,
        )
        assert proj.propagated == 1  # SPEAKER_01 déjà validé → intouché
        assert proj.spk_stats[0]["mapped_name"] == "Anna"
        assert proj.spk_stats[1]["mapped_name"] == "Nom Validé"
        assert proj.mapping_changed is True
        assert proj.spk_map["SPEAKER_00"]["name"] == "Anna"

    def test_role_absent_ignore_le_locuteur(self):
        proj = sp.apply_speaker_roles({"SPEAKER_00": {"label": "Anna", "role": ""}}, [], {}, {})
        assert proj.created == 0 and proj.updated == 0 and proj.participants == []


class TestNormalizeSpeakerRoleInfo:
    def test_split_label_dans_le_role(self):
        assert sp.normalize_speaker_role_info({"label": "", "role": "Anna — animatrice"}) == {
            "label": "Anna", "role": "animatrice",
        }

    def test_deja_normalise(self):
        assert sp.normalize_speaker_role_info({"label": "Anna", "role": "animatrice"}) == {
            "label": "Anna", "role": "animatrice",
        }


class TestBuildLabeledSegments:
    _TURNS = {"turns": [
        {"speaker": "SPEAKER_00", "start": 0.0, "end": 5.0},
        {"speaker": "SPEAKER_01", "start": 5.0, "end": 10.0},
    ]}

    def test_attribue_un_segment_mono_locuteur(self):
        segments = [{"text": "Bonjour à tous", "start": 1.0, "end": 4.0}]
        assert sp.build_labeled_segments(segments, self._TURNS) == [("SPEAKER_00", "Bonjour à tous")]

    def test_ignore_un_segment_a_deux_voix(self):
        segments = [{"text": "Chevauchement", "start": 4.0, "end": 6.0}]
        assert sp.build_labeled_segments(segments, self._TURNS) == []

    def test_entrees_vides(self):
        assert sp.build_labeled_segments([], self._TURNS) == []
        assert sp.build_labeled_segments([{"text": "x", "start": 0, "end": 1}], {"turns": []}) == []


class TestInjectSpeakerGenders:
    def test_normalise_le_format_string_et_remplit_le_genre(self):
        genders = {"SPEAKER_00": {"gender": "female", "male_s": 0.0, "female_s": 4.2}}
        stats_data = {
            "speakers": ["SPEAKER_00"],
            "stats": {"SPEAKER_00": {"speaking_time_seconds": 12, "turn_count": 3}},
        }
        spk_stats, updated = sp.inject_speaker_genders(genders, stats_data)
        assert updated == 1
        assert spk_stats[0]["gender"] == "female"
        assert spk_stats[0]["speaking_time_seconds"] == 12

    def test_ne_touche_pas_un_genre_utilisateur(self):
        genders = {"SPEAKER_00": {"gender": "female", "male_s": 0.0, "female_s": 4.2}}
        spk_stats, updated = sp.inject_speaker_genders(
            genders, {"speakers": [{"speaker_id": "SPEAKER_00", "gender": "male"}]}
        )
        assert updated == 0 and spk_stats[0]["gender"] == "male"


class TestRenderDiarizationContext:
    def test_aucun_locuteur_rend_none(self):
        assert sp.render_diarization_context([], {"speakers": []}) is None

    def test_rendu_complet_avec_genres(self):
        speakers_result = {
            "speakers": [
                {"speaker_id": "SPEAKER_00", "speaking_time_seconds": 60, "turn_count": 5},
                {"speaker_id": "SPEAKER_01", "speaking_time_seconds": 30, "turn_count": 2},
            ],
            "turns": [{"speaker": "SPEAKER_00", "start": 0.0, "end": 5.0}],
        }
        segments = [{"text": "Goûtez notre fromage, Bertrand", "start": 1.0, "end": 4.0}]
        genders = {"SPEAKER_00": {"gender": "male", "male_s": 4.0, "female_s": 0.1}}
        content = sp.render_diarization_context(segments, speakers_result, None, genders)

        assert content is not None
        assert "**Nombre de locuteurs détectés :** 2" in content
        assert "**[SPEAKER_00]** Goûtez notre fromage, Bertrand" in content
        assert "## Genre vocal par locuteur (estimation acoustique)" in content
        assert "**Consigne :**" in content

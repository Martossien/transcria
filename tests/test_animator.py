"""Animateur validé par l'humain — de la case cochée jusqu'au prompt.

Demande utilisateur (2026-08-21) : « quand quelqu'un anime une réunion, il doit être
mieux pris en compte ». Le drapeau ``is_animator`` existait dans le modèle et dans le
Word depuis longtemps, mais **rien ne pouvait le poser** et **aucune LLM ne le voyait**
(le contexte de job le laissait tomber). Ces tests verrouillent la chaîne complète.

Doctrine verrouillée ici aussi, et c'est le point délicat : l'animateur est un **fil de
structure**, jamais une autorité de contenu. Le prompt de résumé porte la règle depuis
la v2.7 (« ne jamais écouter davantage l'animateur ») ; on lui donne la donnée, pas un
droit de vote supplémentaire.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from transcria.context.job_context_builder import JobContextBuilder
from transcria.context.meeting_context import summary_untouched_by_human
from transcria.context.participants import ParticipantsManager
from transcria.exports.docx_report import neutral_speaker_names
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job, JobState
from transcria.llm_tools.llm_parsing import parse_summary_term_line, strip_variant_comment
from transcria.llm_tools.prompt_locator import build_harmonization_glossary
from transcria.web.pages_routes import _apply_animator_suggestion
from transcria.workflow.animator_hint import animator_from_roles, suggest_animator
from transcria.workflow.phases.correction import (
    _persist_reanchored_summary,
    reanchored_summary_error,
    sans_formes_ambigues,
    summary_to_reanchor,
)
from transcria.workflow.phases.summary_llm import paragraph_floor, synthese_shortfall
from transcria.workflow.speaker_projection import is_generic_label

ROOT = Path(__file__).resolve().parents[1]


def _job(job_id: str = "j-anim") -> Job:
    return Job(id=job_id, owner_id="u1", title="Test", state=JobState.CREATED.value)


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


class TestContexteDeJob:
    def test_animateur_valide_atteint_la_llm(self, tmp_dir):
        """Le trou d'origine : le drapeau s'arrêtait à participants.json."""
        job = _job()
        ParticipantsManager.save(job, tmp_dir, [
            {"name": "Alice", "function": "Cheffe de projet", "role": "anime",
             "is_animator": True},
            {"name": "Bob", "function": "Dev", "role": "contribue"},
        ])

        participants = JobContextBuilder.build(job, tmp_dir)["participants"]

        assert participants[0]["is_animator"] is True

    def test_sans_animateur_le_contexte_est_identique_a_avant(self, tmp_dir):
        """Une réunion sans animateur ne doit RIEN changer à ce que reçoit la LLM :
        la clé est absente, pas posée à ``false`` (sinon on introduit du bruit dans
        chaque contexte, et une notion là où l'utilisateur n'a rien dit)."""
        job = _job("j-anim-none")
        ParticipantsManager.save(job, tmp_dir, [{"name": "Alice", "function": "Dev"}])

        participants = JobContextBuilder.build(job, tmp_dir)["participants"]

        assert "is_animator" not in participants[0]


class TestEtape5:
    """La case doit exister dans les TROIS branches de l'étape 5 : locuteurs diarisés,
    participants déjà enregistrés, participants seulement suggérés par la LLM. Une seule
    branche câblée = un utilisateur qui ne peut pas cocher selon d'où vient sa liste."""

    def test_les_trois_branches_portent_la_case(self):
        template = (ROOT / "transcria/web/templates/wizard/_step_participants.html").read_text(
            encoding="utf-8")

        assert template.count('class="form-check-input speaker-animator"') == 3

    def test_la_case_se_rouvre_cochee(self):
        template = (ROOT / "transcria/web/templates/wizard/_step_participants.html").read_text(
            encoding="utf-8")

        assert "s.get('mapped_is_animator')" in template
        assert "{% if p.is_animator %} checked{% endif %}" in template

    def test_le_js_envoie_la_case_et_non_false_en_dur(self):
        """Régression d'origine : ``is_animator: false`` était écrit en dur."""
        js = (ROOT / "transcria/web/static/js/wizard.js").read_text(encoding="utf-8")

        assert "is_animator: isAnimator" in js
        assert "is_animator: false" not in js
        assert "querySelector('.speaker-animator')" in js


class TestSuggestion:
    """Lot 2 — la machine PROPOSE, l'humain dispose.

    Un seul signal, et c'est délibéré : le **rôle annoncé** par la LLM de résumé, vérifié
    sur les réunions réelles disponibles (4 déclenchements sur 16 réunions portant des
    rôles, tous corrects, aucun faux positif sur les dialogues à deux). Le chemin
    « deviner à la forme des tours de parole » a été essayé puis RETIRÉ : rejoué sur le
    corpus réel il ne se déclenchait jamais, et sur la seule vraie réunion à quatre voix
    aucune mesure ne départage personne (cf. l'en-tête du module).
    """

    def test_le_role_annonce_designe_l_animateur(self):
        hint = suggest_animator({
            "SPEAKER_00": {"label": "Conseillère", "role": "pose des objections"},
            "SPEAKER_01": {"label": "Claire", "role": "anime la séance, soumet au vote"},
        })

        assert hint is not None
        assert (hint.speaker_id, hint.reason, hint.matched) == ("SPEAKER_01", "role", "anime")

    def test_le_role_peut_etre_dans_le_libelle(self):
        """Vu tel quel sur le corpus réel : la LLM écrit « Maire / Animateur » en libellé
        et décrit tout autre chose dans le champ rôle."""
        hint = suggest_animator({"SPEAKER_01": {"label": "Maire / Animateur",
                                                "role": "expose les délibérations"}})

        assert hint is not None and hint.speaker_id == "SPEAKER_01"

    def test_role_annonce_dans_les_cinq_langues(self):
        for role in ("Animatrice de la réunion", "facilitator", "Moderation der Sitzung",
                     "moderadora", "moderatrice"):
            hint = animator_from_roles({"SPEAKER_01": {"label": "X", "role": role}})
            assert hint is not None and hint.speaker_id == "SPEAKER_01"

    def test_le_libelle_prime_sur_la_description(self):
        """Vécu au 4ᵉ parcours réel : un locuteur PORTE le mot dans son identité
        (« Animateur / Secrétaire de séance »), l'autre l'a seulement dans ce qu'il fait
        (« anime la discussion »). Compter les deux à égalité faisait abstenir l'outil
        alors que la réponse était nette."""
        hint = suggest_animator({
            "SPEAKER_00": {"label": "Présentateur / Membre du CSE",
                           "role": "présente les aménagements, anime la discussion"},
            "SPEAKER_01": {"label": "Animateur / Secrétaire de séance",
                           "role": "ouvre la séance, ferme la porte"},
        })

        assert hint is not None and hint.speaker_id == "SPEAKER_01"

    def test_deux_libelles_d_animateur_ne_departagent_rien(self):
        """L'ambiguïté au niveau le plus fort ne se rattrape pas au niveau du dessous."""
        assert animator_from_roles({
            "SPEAKER_00": {"label": "Animateur", "role": "ouvre la séance"},
            "SPEAKER_01": {"label": "Animatrice", "role": "distribue la parole"},
        }) is None

    def test_la_description_sert_de_repli_quand_aucun_libelle_ne_tranche(self):
        hint = suggest_animator({
            "SPEAKER_00": {"label": "Marie Dupont", "role": "anime la séance"},
            "SPEAKER_01": {"label": "Jean Martin", "role": "pose des questions"},
        })

        assert hint is not None and hint.speaker_id == "SPEAKER_00"

    def test_deux_animateurs_annonces_ne_departagent_rien(self):
        hints = {"SPEAKER_00": {"role": "animateur"}, "SPEAKER_01": {"role": "animatrice"}}

        assert animator_from_roles(hints) is None

    def test_un_role_ordinaire_ne_declenche_rien(self):
        """Vocabulaire FERMÉ : « présidente » (de l'entreprise) ou « formateur » décrivent
        autre chose que l'animation d'une séance. Vérifié sur le corpus réel : les
        dialogues vendeur/client et podcast ne déclenchent rien."""
        for role in ("Présidente", "formateur", "organisateur", "chef de projet",
                     "sert le client, propose des dégustations", "pose des questions"):
            assert animator_from_roles({"SPEAKER_01": {"role": role}}) is None

    def test_donnees_absentes_ou_cassees_ne_levent_pas(self):
        assert suggest_animator() is None
        assert suggest_animator({}) is None
        assert suggest_animator({"SPEAKER_00": None}) is None
        assert suggest_animator({"SPEAKER_00": "animateur"}) is not None  # ancien format


class TestBranchementEtape5:
    """Le service pur ne sert à rien s'il n'arrive pas jusqu'à l'écran."""

    def test_la_suggestion_est_posee_sur_le_bon_locuteur(self, tmp_dir):
        fs = JobFilesystem(tmp_dir, "j-anim-ui")
        speakers = {"speakers": [{"speaker_id": "SPEAKER_00"}, {"speaker_id": "SPEAKER_01"}]}

        _apply_animator_suggestion(
            fs, speakers, {"SPEAKER_01": {"label": "Claire", "role": "anime la séance"}}, [])

        assert speakers["speakers"][1]["animator_suggested"] is True
        assert speakers["speakers"][1]["animator_reason"] == "role"
        assert "animator_suggested" not in speakers["speakers"][0]
        # Audit rejouable écrit une fois (seuils + scores), comme pour le manifeste.
        assert fs.load_json("metadata/animator_hint.json")["speaker_id"] == "SPEAKER_01"

    def test_un_animateur_deja_valide_fait_taire_la_suggestion(self, tmp_dir):
        """Le choix humain ne se re-discute pas à chaque rechargement de page."""
        fs = JobFilesystem(tmp_dir, "j-anim-deja")
        speakers = {"speakers": [{"speaker_id": "SPEAKER_00"}]}

        _apply_animator_suggestion(
            fs, speakers, {"SPEAKER_00": {"role": "animateur"}},
            [{"name": "Claire", "is_animator": True}])

        assert "animator_suggested" not in speakers["speakers"][0]

    def test_le_bouton_ne_coche_pas_d_office(self):
        template = (ROOT / "transcria/web/templates/wizard/_step_participants.html").read_text(
            encoding="utf-8")
        js = (ROOT / "transcria/web/static/js/wizard.js").read_text(encoding="utf-8")

        # La suggestion est un BOUTON à cliquer, pas un `checked` conditionnel.
        assert "TranscrIA.applyAnimatorSuggestion" in template
        assert "s.get('animator_suggested')" in template
        assert "{% if s.get('animator_suggested') %} checked" not in template
        assert "W.applyAnimatorSuggestion" in js


class TestReancrageSynthese:
    """Lot 1bis — la synthèse est réancrée sur le SRT corrigé PAR LA PASSE QUI LE RELIT.

    Constat qui motive le lot : la synthèse livrée est rédigée sur `quick_transcript.txt`,
    la transcription RAPIDE d'avant correction — jamais sur le SRT qu'on livre à côté. La
    passe de correction est la seule qui relit tout le SRT ET reçoit déjà le contexte
    validé (animateur compris) : le réancrage n'y coûte que la génération, contre une
    passe complète (≈ 1× la durée de l'audio) pour un ré-résumé autonome.
    """

    _HEADINGS = ["## Synthèse", "## Summary"]

    def test_la_synthese_non_editee_part_au_reancrage(self):
        ctx = {"summary_llm": "# Résumé\n\n## Synthèse\nDeux points discutés.\n\n## Termes\n(aucun)",
               "summary": "Deux points discutés."}

        assert summary_to_reanchor(ctx, self._HEADINGS) == "Deux points discutés."

    def test_une_synthese_EDITEE_par_l_humain_n_est_jamais_reecrite(self):
        """Et on ne la demande même pas à la LLM : coût nul, risque nul."""
        ctx = {"summary_llm": "## Synthèse\nDeux points discutés.",
               "summary": "Deux points discutés, dont le budget que j'ai ajouté."}

        assert summary_to_reanchor(ctx, self._HEADINGS) == ""

    def test_sans_synthese_il_n_y_a_rien_a_reancrer(self):
        assert summary_to_reanchor({}, self._HEADINGS) == ""
        assert summary_to_reanchor({"summary_llm": ""}, self._HEADINGS) == ""

    def test_la_garde_rejette_une_synthese_qui_fond(self):
        """Même doctrine que le SRT : le prompt EXIGE, le code VÉRIFIE. C'est la dérive
        observée quand on laisse un modèle « réécrire » librement."""
        precedente = "x" * 1000

        assert reanchored_summary_error(precedente, "x" * 400) is not None
        assert reanchored_summary_error(precedente, "x" * 1500) is not None
        assert reanchored_summary_error(precedente, "x" * 950) is None

    def test_la_garde_rejette_un_fichier_qui_n_est_pas_une_synthese(self):
        """Vécu ailleurs : l'agent recrache des lignes de SRT dans le mauvais fichier."""
        srt = "1\n00:00:01,000 --> 00:00:04,000\nBonjour."

        assert "SRT" in (reanchored_summary_error("x" * 60, srt) or "")
        assert reanchored_summary_error("x" * 60, "   ") is not None

    def test_la_regle_est_repliquee_dans_les_cinq_prompts(self):
        """Une consigne ajoutée au FR et oubliée ailleurs = une fonctionnalité qui
        n'existe que pour les livrables français."""
        fr = (ROOT / "configs/prompts/correction_prompt.txt").read_text(encoding="utf-8")
        assert "synthese_a_reancrer.md" in fr and "summary_reancree.md" in fr
        for loc in ("en", "de", "es", "it"):
            texte = (ROOT / f"configs/prompts/{loc}/correction_prompt.txt").read_text(encoding="utf-8")
            assert "synthese_a_reancrer.md" in texte, f"{loc} : entrée absente"
            assert "summary_reancree.md" in texte, f"{loc} : sortie absente"
            assert "is_animator" in texte, f"{loc} : règle animateur absente"

    def test_le_reancrage_est_demande_APRES_le_srt(self):
        """Le SRT est l'artefact critique : la re-synthèse est une étape finale, jamais un
        second objectif mené de front (un agent à qui on demande deux choses fait moins
        bien les deux)."""
        source = (ROOT / "transcria/llm_tools/opencode_runner.py").read_text(encoding="utf-8")
        bloc = source.split("def run_correction(", 1)[1].split("def ", 1)[0]

        assert "ENSUITE SEULEMENT" in bloc
        assert bloc.index("transcription_corrigee.srt") < bloc.index("summary_reancree.md")


class TestLisibiliteDuCompteRendu:
    """Trois défauts vus en LISANT le Word produit par le parcours réel.

    Aucun ne cassait quoi que ce soit — ils rendaient le document moins juste : une
    suggestion impossible à juger sans réécouter, un mot générique qui se lit comme un
    nom propre, et une synthèse qui hésite sur ce que l'utilisateur a déjà validé.
    """

    def test_l_info_bulle_montre_le_role_annonce(self):
        """La suggestion s'est trompée 1 fois sur 2 au banc : l'utilisateur doit pouvoir
        la juger d'un survol, sans réécouter l'audio."""
        template = (ROOT / "transcria/web/templates/wizard/_step_participants.html").read_text(
            encoding="utf-8")

        assert "Rôle annoncé : %(role)s" in template
        # Le champ Rôle fait 150 px : son contenu est toujours coupé à l'écran.
        assert 'title="{{ s.mapped_role or \'\' }}"' in template

    def test_un_mot_generique_n_est_pas_un_nom(self):
        for generique in ("Participant", "participante", "Speaker", "Teilnehmerin",
                          "membro", "Invité", "inconnu"):
            assert is_generic_label(generique), generique

    def test_un_libelle_informatif_est_conserve(self):
        """« Animateur / Secrétaire du CSE » dit quelque chose : on ne l'efface pas."""
        for utile in ("Animateur / Secrétaire du CSE", "Maire / Animateur",
                      "Marie Dupont", "Cheffe de projet", ""):
            assert not is_generic_label(utile), utile

    def test_l_identifiant_de_diarisation_devient_lisible(self):
        noms = neutral_speaker_names(
            ["SPEAKER_01", "SPEAKER_00", "Marie Dupont", "", None], "Locuteur {n}")

        assert noms == {"SPEAKER_00": "Locuteur 1", "SPEAKER_01": "Locuteur 2"}

    def test_la_numerotation_est_stable_entre_deux_rendus(self):
        """Un compte rendu régénéré ne doit pas renuméroter ses locuteurs."""
        a = neutral_speaker_names(["SPEAKER_02", "SPEAKER_00"], "Locuteur {n}")
        b = neutral_speaker_names(["SPEAKER_00", "SPEAKER_02"], "Locuteur {n}")

        assert a == b

    def test_les_cinq_langues_ont_leur_libellé_neutre(self):
        from transcria.exports.docx_style import _DOCX_LABELS

        for lang, labels in _DOCX_LABELS.items():
            assert "{n}" in labels.get("speaker_n", ""), f"{lang} : libellé neutre absent"

    def test_la_regle_anti_hesitation_est_dans_les_cinq_prompts(self):
        fr = (ROOT / "configs/prompts/correction_prompt.txt").read_text(encoding="utf-8")
        assert "faits\n   VALIDÉS" in fr or "VALIDÉS" in fr
        for loc, mot in (("en", "VALIDATED"), ("de", "BESTÄTIGTE"),
                         ("es", "VALIDADOS"), ("it", "CONVALIDATI")):
            texte = (ROOT / f"configs/prompts/{loc}/correction_prompt.txt").read_text(encoding="utf-8")
            assert mot in texte, f"{loc} : règle anti-hésitation absente"

    def test_un_nom_de_role_nu_ne_designe_personne_non_plus(self):
        """Vu au 3ᵉ parcours réel : la LLM a proposé « Animateur » tout court comme nom,
        et ce mot se retrouvait devant 14 répliques. La colonne « Rôle » dit déjà ce que
        la personne fait — le nom, lui, doit désigner QUELQU'UN."""
        for role_nu in ("Animateur", "Président", "Chairperson", "Moderatorin",
                        "Secrétaire", "Presentatore"):
            assert is_generic_label(role_nu), role_nu

    def test_un_libelle_compose_reste_informatif(self):
        for compose in ("Présentateur / Animateur secondaire", "Animateur / Secrétaire du CSE",
                        "Maire / Animateur", "Cheffe de projet"):
            assert not is_generic_label(compose), compose

    def test_sans_nom_le_compte_rendu_ne_montre_pas_un_tiret(self):
        """Le repli est l'identifiant du locuteur — que le rendu présente en « Locuteur N »
        — et non un tiret muet qui perd l'information « c'est une voix distincte »."""
        source = (ROOT / "transcria/exports/docx_report.py").read_text(encoding="utf-8")
        bloc = source.split("def _merge_participants", 1)[1].split("def ", 1)[0]

        assert 'spk.get("speaker_id")' in bloc

    def test_le_prefill_de_l_etape_5_filtre_aussi_les_libelles_generiques(self):
        """Trouvé au 4ᵉ parcours réel : la projection refusait « Animateur » comme nom,
        mais le chemin d'AFFICHAGE le pré-remplissait quand même dans le champ « Nom » —
        l'utilisateur validait le mot et il repartait en base. Deux portes, une seule
        était fermée."""
        source = (ROOT / "transcria/web/pages_routes.py").read_text(encoding="utf-8")
        bloc = source.split("elif speaker_role_hints:", 1)[1].split("# Vague 2", 1)[0]

        assert "is_generic_label" in bloc


class TestBancDesRoles:
    """Ce que le banc du 2026-08-22 a mesuré sur des réunions RÉELLES (4 sources
    distinctes, 8 passes, plus 9 passes déjà en base).

    Enseignement principal : la suggestion est **stable et juste sur des réunions de
    10-15 min** (conseil municipal, réunion de projet, séance CSE) et **instable sur des
    extraits de 5 min**, où la structure de la séance n'est pas visible. Le signal n'est
    donc pas mauvais — c'est la matière courte qui l'est.
    """

    def test_un_verbe_d_animation_situe_est_reconnu(self):
        """Sur une réunion de projet, la LLM étiquette « Manager / Lead » — aucun mot
        d'animation dans l'identité — mais décrit « dirige la réunion » dans le rôle.
        L'outil s'abstenait ; il désigne maintenant la bonne personne, dans les 2 passes."""
        hint = suggest_animator({
            "SPEAKER_01": {"label": "Manager / Lead",
                           "role": "dirige la réunion, donne des directives techniques"},
            "SPEAKER_02": {"label": "Développeur", "role": "travaille sur le projet"},
        })

        assert hint is not None
        assert (hint.speaker_id, hint.matched) == ("SPEAKER_01", "dirige la reunion")

    def test_le_verbe_seul_ne_suffit_pas(self):
        """« dirige » tout court désignerait un directeur d'entreprise aussi bien qu'un
        animateur de séance : seules les locutions ENTIÈRES comptent."""
        assert suggest_animator({
            "SPEAKER_00": {"label": "Directeur", "role": "dirige l'entreprise depuis 2019"},
        }) is None

    def test_les_locutions_couvrent_les_cinq_langues(self):
        for role in ("dirige la réunion", "leads the meeting", "leitet die Sitzung",
                     "conduce la reunión", "guida la riunione"):
            hint = suggest_animator({"SPEAKER_00": {"label": "Manager", "role": role}})
            assert hint is not None, role


class TestTermesDouteuxMalformes:
    """Défauts de FORMAT vus sur une vraie réunion métier (1 h 52, 26 termes proposés).

    Ce ne sont pas des hallucinations : la LLM ajoute un groupe en trop ou un commentaire
    là où le gabarit attend une donnée, et le parseur — qui n'admettait qu'une seule
    lecture — encaissait le débordement DANS la forme validée. Or cette forme part dans le
    lexique, donc potentiellement dans le SRT corrigé.
    """

    def test_le_gras_ne_fuit_plus_dans_la_forme_validee(self):
        ligne = ("- **Superviseur** [application] (sigle) (critique) | variantes_suspectes: "
                 "super viseur ; Superviseur | commentaire: outil retiré")

        terme = parse_summary_term_line(ligne)

        assert terme["term"] == "Superviseur"
        assert terme["category"] == "application"
        assert terme["priority"] == "critique"

    def test_un_terme_a_le_droit_d_avoir_des_parentheses(self):
        """On ne coupe que sur la preuve de fuite (le `**` au milieu) : sans elle, un
        terme parenthésé reste intact."""
        terme = parse_summary_term_line("- **Gestion (nationale)** [application] (importante)")

        assert terme["term"] == "Gestion (nationale)"

    def test_une_variante_est_une_forme_entendue_pas_un_commentaire(self):
        assert strip_variant_comment("génie (erreur STT probable)") == "génie"
        assert strip_variant_comment("Tessy (T2SI)") == "Tessy (T2SI)"
        assert strip_variant_comment("Aloha") == "Aloha"

    def test_un_reancrage_demande_mais_non_rendu_est_JOURNALISE(self, tmp_dir, caplog):
        """Vécu sur une réunion de 1 h 52 : l'agent termine le SRT — sa tâche critique —
        et s'arrête avant le réancrage. C'est le comportement prévu (best-effort), mais il
        disparaissait EN SILENCE : rien dans le journal ne disait que la synthèse était
        restée celle de la transcription rapide."""
        import logging
        fs = JobFilesystem(tmp_dir, "j-reancrage-muet")
        fs.save_json("context/meeting_context.json",
                     {"summary_llm": "## Synthèse\nDeux points discutés.", "summary": ""})
        fs.save_text("metadata/transcription_corrigee.srt", "1\n00:00:01,000 --> 00:00:02,000\nBonjour.")
        job = _job("j-reancrage-muet")

        with caplog.at_level(logging.WARNING):
            _persist_reanchored_summary(fs, {"rewritten_summary": ""}, job)

        assert any("réancrage demandé mais non rendu" in r.message for r in caplog.records)


class TestLongueurDeLaSynthese:
    """La synthèse n'était proportionnée à rien.

    Mesuré sur le corpus local avant d'écrire la règle : 4 074 caractères de synthèse pour
    une réunion de 15 min, **4 240 pour une réunion de 1 h 53** — soit 272 car/min d'un
    côté et 38 de l'autre. La consigne « proportionnée à la durée » ne pouvait pas être
    suivie : l'agent ne recevait JAMAIS la durée, il ne voyait qu'un texte.
    """

    def test_le_plancher_suit_la_duree(self):
        assert paragraph_floor(5) == 3        # réunion courte : plancher bas
        assert paragraph_floor(15) == 3
        assert paragraph_floor(60) == 4
        assert paragraph_floor(113) == 7      # la réunion du banc : 6 rendus, 7 attendus

    def test_l_ordre_du_jour_releve_le_plancher(self):
        """Cinq points à l'ordre du jour méritent au moins cinq paragraphes."""
        assert paragraph_floor(20, agenda_items=5) == 5

    def test_le_manque_est_chiffre(self):
        rendus, plancher = synthese_shortfall("un\n\ndeux\n\ntrois\n\nquatre\n\ncinq\n\nsix",
                                              minutes=113, agenda_items=5)

        assert (rendus, plancher) == (6, 7)

    def test_la_durée_est_transmise_a_l_agent(self):
        """Sans elle, « proportionnée à la durée » est une consigne creuse."""
        source = (ROOT / "transcria/llm_tools/opencode_runner.py").read_text(encoding="utf-8")
        bloc = source.split("def run_summary(", 1)[1].split("\n    def ", 1)[0]

        assert "audio_duration_s" in bloc
        assert "La réunion dure" in bloc

    def test_la_regle_de_longueur_est_dans_les_cinq_prompts(self):
        mots = {"": "paragraphe", "en": "paragraph", "de": "Absatz",
                "es": "párrafo", "it": "paragrafo"}
        for loc, mot in mots.items():
            chemin = f"configs/prompts/{loc + '/' if loc else ''}summary_prompt.txt"
            texte = (ROOT / chemin).read_text(encoding="utf-8")

            assert mot in texte, f"{chemin} : le mot « {mot} » manque"
            assert "15" in texte, f"{chemin} : la tranche de 15 minutes manque"


class TestSyntheseHarmoniseeQuiArriveEnfin:
    """La relecture finale tournait pour rien sur la synthèse.

    Sa tâche A harmonise la synthèse sur le glossaire validé — 1,69× la durée de l'audio —
    et écrit `summary_harmonized`, qui est TROISIÈME dans l'ordre de priorité des
    livrables, derrière `summary`. Or l'étape 4 remplit systématiquement `summary` avec le
    préremplissage : le résultat de la passe n'atteignait jamais le document.
    """

    _HEADINGS = ["## Synthèse", "## Summary"]

    def test_une_synthese_non_editee_peut_etre_remplacee(self):
        ctx = {"summary_llm": "# R\n\n## Synthèse\nDeux points.\n\n## Termes\n(aucun)",
               "summary": "Deux points."}

        assert summary_untouched_by_human(ctx, self._HEADINGS) is True

    def test_une_synthese_ECRITE_par_l_humain_est_intouchable(self):
        ctx = {"summary_llm": "## Synthèse\nDeux points.",
               "summary": "Deux points, plus le budget que j'ai ajouté."}

        assert summary_untouched_by_human(ctx, self._HEADINGS) is False

    def test_un_champ_vide_laisse_la_main_aux_passes_suivantes(self):
        assert summary_untouched_by_human({"summary_llm": "## Synthèse\nX", "summary": ""},
                                          self._HEADINGS) is True

    def test_la_relecture_finale_reinjecte_dans_le_champ_livre(self):
        source = (ROOT / "transcria/workflow/phases/final_review.py").read_text(encoding="utf-8")

        assert "summary_untouched_by_human" in source
        # La réinjection vient APRÈS l'écriture de summary_harmonized, pas à sa place.
        assert (source.index('meeting_ctx["summary_harmonized"]')
                < source.index('meeting_ctx["summary"] = synthese'))

    def test_une_amelioration_MACHINE_n_est_pas_une_ecriture_humaine(self):
        """Rejeu du 2026-08-22 : la correction avait réancré la synthèse, et la relecture
        finale a ensuite renoncé à appliquer la sienne — elle prenait l'amélioration
        machine pour une écriture humaine. La trace de provenance tranche."""
        ctx = {"summary_llm": "## Synthèse\nTexte d'origine.",
               "summary": "Texte réancré sur le SRT corrigé.",
               "summary_machine": "Texte réancré sur le SRT corrigé."}

        assert summary_untouched_by_human(ctx, ["## Synthèse"]) is True

    def test_une_ecriture_humaine_apres_une_passe_machine_reste_souveraine(self):
        ctx = {"summary_llm": "## Synthèse\nTexte d'origine.",
               "summary": "Texte réancré, PLUS ma phrase à moi.",
               "summary_machine": "Texte réancré sur le SRT corrigé."}

        assert summary_untouched_by_human(ctx, ["## Synthèse"]) is False


class TestFormeValideeAmbigue:
    """« Parafeur / Éditeur / Sigle » n'est pas une graphie, c'est une hésitation.

    Mesuré sur le corpus : **28 formes validées sur 192 contiennent un « / »**, et certaines
    fusionnent des concepts DISTINCTS (deux outils, deux rôles, deux années). Appliquée
    mécaniquement lors d'un rejeu réel, l'une d'elles a remplacé un nom de FOURNISSEUR par le
    nom de la fonction qu'il rend — la phrase affirmait ensuite quelque chose de faux.
    """

    def test_le_glossaire_signale_la_forme_ambigue(self):
        glossaire = build_harmonization_glossary([], [
            {"term": "Parafeur / Éditeur / Sigle", "variants": ["Éditeur"]},
            {"term": "Passerelle", "variants": ["passe-relle"]},
        ])

        ambigue = [ligne for ligne in glossaire.splitlines() if "Parafeur" in ligne][0]
        nette = [ligne for ligne in glossaire.splitlines() if "Passerelle" in ligne][0]
        assert "NE PAS substituer" in ambigue
        assert "NE PAS substituer" not in nette

    def test_un_slash_sans_espaces_n_est_pas_une_alternative(self):
        """« 24/7 », « E/S » : un slash collé fait partie du terme, pas d'ambiguïté."""
        glossaire = build_harmonization_glossary([], [{"term": "support 24/7", "variants": []}])

        assert "NE PAS substituer" not in glossaire

    def test_la_regle_est_dans_les_trois_prompts_et_cinq_langues(self):
        """Une règle écrite en français seulement ne protège que les livrables français."""
        marqueurs = {"": ("fournisseur", "ambigu"), "en/": ("VENDOR", "mbiguous"),
                     "de/": ("Anbieternamen", "ehrdeutig"), "es/": ("PROVEEDOR", "mbigua"),
                     "it/": ("FORNITORE", "mbigua")}
        for gabarit in ("correction_prompt.txt", "final_review_prompt.txt"):
            for loc, (mot, ambig) in marqueurs.items():
                texte = (ROOT / f"configs/prompts/{loc}{gabarit}").read_text(encoding="utf-8")
                assert mot.lower() in texte.lower(), f"{loc}{gabarit} : « {mot} » absent"
                assert ambig.lower() in texte.lower(), f"{loc}{gabarit} : ambiguïté non traitée"
        # La règle correspondante À LA SOURCE (« une seule forme de référence ») a été
        # RETIRÉE après mesure : sur six parcours d'une même réunion, le nombre d'entrées
        # ambiguës ne bouge pas (12, 12 avec la règle ; 11, 18, 4 sans). Elle n'agissait pas
        # sur ce qu'elle prétendait traiter, et une règle inerte dilue celles qui servent.
        # Ce qui protège vraiment est le garde-fou à l'USAGE, doublé d'un refus en code
        # (voir TestRefusDesFormesAmbigues) : une forme « A / B » n'est jamais substituée.


class TestRefusDesFormesAmbigues:
    """E1 du banc des règles (2026-08-22) — la seule garde qui ait tenu est mécanique.

    Deux consignes de prompt avaient été essayées avant, aucune n'a tenu. Mesure sur trois
    rejeux de la phase de correction, avant puis après ce refus en code :

    | | segments modifiés | substitutions vers une forme ambiguë |
    |---|---|---|
    | référence | 42 · 35 · 44 | 18 · 11 · 23 |
    | avec refus | 0 · 41 · 31 | 0 · 10 · 9 |

    Les remplacements restants ne viennent PAS du lexique : l'agent normalise d'après le
    contexte, et la lecture des segments montre que ces corrections-là sont justes.
    """

    def test_une_forme_a_plusieurs_graphies_ne_pilote_plus_de_remplacement(self):
        lexique = [
            {"term": "Fonction / Éditeur / Sigle", "variants": ["Éditeur"]},
            {"term": "Passerelle", "variants": ["passe-relle"]},
        ]

        gardees, ecartees = sans_formes_ambigues(lexique)

        assert [t["term"] for t in gardees] == ["Passerelle"]
        assert ecartees == 1

    def test_un_slash_colle_fait_partie_du_terme(self):
        """« support 24/7 », « E/S » : pas d'alternative, donc pas d'écartement."""
        gardees, ecartees = sans_formes_ambigues([{"term": "support 24/7", "variants": []}])

        assert ecartees == 0 and len(gardees) == 1

    def test_l_entree_ecartee_reste_dans_le_lexique_de_session(self):
        """On lui retire le pouvoir de substituer, pas sa visibilité : l'humain doit
        continuer à la voir dans les points à vérifier."""
        source = (ROOT / "transcria/workflow/phases/correction.py").read_text(encoding="utf-8")
        bloc = source.split("def _prefilter_lexicon", 1)[1].split("\ndef ", 1)[0]

        # Le filtrage porte sur la copie transmise à l'agent, jamais sur le canonique.
        assert "session_lexicon_filtered.json" in bloc
        assert 'fs.save_json("context/session_lexicon.json"' not in bloc

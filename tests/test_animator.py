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
from transcria.context.participants import ParticipantsManager
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job, JobState
from transcria.web.pages_routes import _apply_animator_suggestion
from transcria.workflow.animator_hint import (
    animator_from_roles,
    score_speakers,
    suggest_animator,
)

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

    Les seuils sont volontairement sévères : sur ce champ, une absence de suggestion est
    sans conséquence, alors qu'une mauvaise suggestion se fait valider par habitude et
    entre ensuite dans le compte-rendu comme un fait validé.
    """

    @staticmethod
    def _alternance(animateur: str, autres: list[str], tours: int = 4) -> list[dict]:
        """Réunion animée : l'animateur relance brièvement entre chaque intervention."""
        turns: list[dict] = []
        t = 0.0
        for i in range(tours):
            for autre in autres:
                turns.append({"start": t, "end": t + 4, "speaker": animateur})
                turns.append({"start": t + 4, "end": t + 44, "speaker": autre})
                t += 44
        return turns

    def test_le_noeud_de_la_conversation_est_propose(self):
        hint = suggest_animator(self._alternance("SPEAKER_00", ["SPEAKER_01", "SPEAKER_02"]))

        assert hint is not None
        assert hint.speaker_id == "SPEAKER_00"
        assert hint.reason == "turns"

    def test_un_tour_de_table_ne_propose_personne(self):
        """A→B→C→A→B→C : personne ne distribue la parole, tout le monde se ressemble."""
        turns = [{"start": i * 30.0, "end": i * 30.0 + 30, "speaker": f"SPEAKER_0{i % 3}"}
                 for i in range(12)]

        assert suggest_animator(turns) is None

    def test_le_bavard_nerveux_n_est_pas_un_animateur(self):
        """Le cas que la seule statistique de rythme raterait : SPEAKER_00 intervient
        sans arrêt et brièvement (meilleur rythme, score et écart au-dessus des seuils)
        mais rend TOUJOURS la main à la même personne. Il n'anime rien : c'est la
        diversité, vérifiée seule, qui le recale."""
        turns = []
        t = 0.0
        for i in range(6):
            turns.append({"start": t, "end": t + 5, "speaker": "SPEAKER_00"})
            turns.append({"start": t + 5, "end": t + 25, "speaker": "SPEAKER_01"})
            t += 25
            if i % 3 == 2:  # un troisième locuteur, à qui SPEAKER_00 ne passe jamais la main
                turns.append({"start": t, "end": t + 20, "speaker": "SPEAKER_02"})
                turns.append({"start": t + 20, "end": t + 40, "speaker": "SPEAKER_01"})
                t += 40

        scores = score_speakers(turns)
        assert scores["SPEAKER_00"] == max(scores.values())   # il gagne au score…
        assert suggest_animator(turns) is None                # … et n'est pourtant pas proposé

    def test_deux_voix_ne_donnent_pas_lieu_a_suggestion(self):
        turns = [{"start": i * 20.0, "end": i * 20.0 + 20, "speaker": f"SPEAKER_0{i % 2}"}
                 for i in range(20)]

        assert suggest_animator(turns) is None

    def test_trop_peu_de_tours_pour_lire_une_forme(self):
        turns = self._alternance("SPEAKER_00", ["SPEAKER_01", "SPEAKER_02"], tours=1)

        assert len(turns) < 8
        assert suggest_animator(turns) is None

    def test_le_role_annonce_prime_sur_la_statistique(self):
        """La LLM a LU la réunion ; la forme des tours n'est qu'une régularité."""
        turns = self._alternance("SPEAKER_00", ["SPEAKER_01", "SPEAKER_02"])

        hint = suggest_animator(turns, {"SPEAKER_02": {"label": "Claire", "role": "anime la séance"}})

        assert hint is not None
        assert (hint.speaker_id, hint.reason) == ("SPEAKER_02", "role")

    def test_role_annonce_dans_les_cinq_langues(self):
        for role in ("Animatrice de la réunion", "facilitator", "Moderation der Sitzung",
                     "moderadora", "moderatrice"):
            assert animator_from_roles({"SPEAKER_01": {"label": "X", "role": role}}) == "SPEAKER_01"

    def test_deux_animateurs_annonces_ne_departagent_rien(self):
        hints = {"SPEAKER_00": {"role": "animateur"}, "SPEAKER_01": {"role": "animatrice"}}

        assert animator_from_roles(hints) is None

    def test_un_role_ordinaire_ne_declenche_rien(self):
        """Vocabulaire FERMÉ : « présidente » (de l'entreprise) ou « formateur » décrivent
        autre chose que l'animation d'une séance."""
        for role in ("Présidente", "formateur", "organisateur", "chef de projet"):
            assert animator_from_roles({"SPEAKER_01": {"role": role}}) is None

    def test_donnees_absentes_ou_cassees_ne_levent_pas(self):
        assert suggest_animator([]) is None
        assert suggest_animator([{"start": "x", "end": None, "speaker": "S"}]) is None
        assert suggest_animator([{"speaker": "S"}], None) is None

    def test_audit_rejouable(self):
        """Les seuils et les scores sont écrits : une suggestion doit pouvoir s'expliquer."""
        hint = suggest_animator(self._alternance("SPEAKER_00", ["SPEAKER_01", "SPEAKER_02"]))

        audit = hint.to_audit_dict()
        assert audit["speaker_id"] == "SPEAKER_00"
        assert audit["scores"]["SPEAKER_00"] > audit["scores"]["SPEAKER_01"]
        assert audit["thresholds"]["min_diversity"] == 0.60


class TestBranchementEtape5:
    """Le service pur ne sert à rien s'il n'arrive pas jusqu'à l'écran."""

    def test_la_suggestion_est_posee_sur_le_bon_locuteur(self, tmp_dir):
        fs = JobFilesystem(tmp_dir, "j-anim-ui")
        speakers = {"speakers": [{"speaker_id": "SPEAKER_00"}, {"speaker_id": "SPEAKER_01"}]}

        _apply_animator_suggestion(
            fs, speakers, {"turns": []},
            {"SPEAKER_01": {"label": "Claire", "role": "anime la séance"}}, [])

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
            fs, speakers, {"turns": []},
            {"SPEAKER_00": {"role": "animateur"}},
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

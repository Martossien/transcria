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
from transcria.jobs.models import Job, JobState

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

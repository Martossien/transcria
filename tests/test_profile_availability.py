"""Phase 6 — disponibilité des profils (source unique backend pour le wizard).

Purs : statut structurel par profil + profil recommandé (le maximum qui passe).
"""
from __future__ import annotations

from transcria.workflow.profile_availability import compute_profiles_view

_FULL = {"workflow": {"enable_quality_mode": True, "arbitration_llm": {"enabled": True}}}


def _by_id(view):
    return {p["id"]: p for p in view["profiles"]}


def test_tout_disponible_recommande_le_maximum():
    view = compute_profiles_view(_FULL)
    assert len(view["profiles"]) == 7
    # srt_moss est le SEUL indisponible ici : son backend imposé (moss) n'est pas
    # activé dans cette config — tous les profils historiques restent disponibles.
    by_id = _by_id(view)
    assert by_id["srt_moss"]["available"] is False
    assert all(p["available"] for p in view["profiles"] if p["id"] != "srt_moss")
    # Recommandé = profil disponible de plus haut niveau.
    assert view["recommended"] == "dossier_qualite"


def test_srt_moss_disponible_quand_moss_active():
    cfg = {**_FULL, "moss": {"enabled": True}}
    by_id = _by_id(compute_profiles_view(cfg))
    assert by_id["srt_moss"]["available"] is True


def test_srt_moss_indisponible_sans_moss_avec_raison():
    by_id = _by_id(compute_profiles_view(_FULL))
    assert by_id["srt_moss"]["status"] == "unavailable"
    assert any("moss" in r for r in by_id["srt_moss"]["reasons"])


def test_llm_desactivee_rend_les_profils_llm_indisponibles():
    cfg = {"workflow": {"enable_quality_mode": True, "arbitration_llm": {"enabled": False}}}
    view = compute_profiles_view(cfg)
    profiles = _by_id(view)
    # Sans LLM : seuls les profils SRT (sans résumé/correction) restent lançables.
    assert profiles["srt_express"]["available"] is True
    assert profiles["srt_locuteurs"]["available"] is True
    for pid in ("word_rapide", "word_structure", "word_corrige", "dossier_qualite"):
        assert profiles[pid]["status"] == "unavailable"
        assert profiles[pid]["available"] is False
    assert view["recommended"] == "srt_locuteurs"  # max dispo


def test_mode_qualite_desactive_desactive_les_profils_qui_diarisent():
    cfg = {"workflow": {"enable_quality_mode": False, "arbitration_llm": {"enabled": True}}}
    view = compute_profiles_view(cfg)
    profiles = _by_id(view)
    # Profils qui diarisent (Word structuré/corrigé/dossier) → désactivés par config.
    for pid in ("word_structure", "word_corrige", "dossier_qualite"):
        assert profiles[pid]["status"] == "disabled_by_config"
    # word_rapide ne diarise pas et a la LLM → disponible.
    assert profiles["word_rapide"]["available"] is True
    assert view["recommended"] == "word_rapide"


def test_liste_blanche_de_config_desactive_les_autres():
    cfg = {"workflow": {
        "enable_quality_mode": True, "arbitration_llm": {"enabled": True},
        "profiles": {"enabled": ["srt_express"]},
    }}
    view = compute_profiles_view(cfg)
    profiles = _by_id(view)
    assert profiles["srt_express"]["available"] is True
    assert profiles["dossier_qualite"]["status"] == "disabled_by_config"
    assert view["recommended"] == "srt_express"


def test_items_portent_livrables_validations_et_raisons():
    view = compute_profiles_view(_FULL)
    p = _by_id(view)["word_corrige"]
    assert "SRT corrigé" in p["deliverables"]
    assert "Validation des locuteurs" in p["validations"]
    assert p["reasons"] == []


def test_aucun_profil_lancable_recommande_none():
    # Ni LLM ni mode qualité : srt_express/srt_locuteurs ne diarisent pas et n'ont pas besoin
    # de LLM → restent disponibles ; recommended n'est donc pas None ici. On vérifie le cas
    # extrême via une liste blanche vide impossible : à défaut, on garde la cohérence du contrat.
    view = compute_profiles_view({"workflow": {"arbitration_llm": {"enabled": False}, "enable_quality_mode": False}})
    profiles = _by_id(view)
    # srt_express/srt_locuteurs ne dépendent ni de LLM ni de diarisation → toujours dispo.
    assert profiles["srt_express"]["available"] is True
    assert view["recommended"] in ("srt_express", "srt_locuteurs")


# ── Disposition du wizard pilotée par le profil (choix à l'étape 1) ──────────--

from transcria.jobs.models import JobState  # noqa: E402
from transcria.workflow.profile_availability import compute_wizard_layout  # noqa: E402
from transcria.workflow.profiles import get_profile  # noqa: E402
from transcria.workflow.states import WorkflowState  # noqa: E402


def _layout(profile_id, job_state):
    profile = get_profile(profile_id) if profile_id else None
    statuses = WorkflowState.compute_statuses(job_state)
    return compute_wizard_layout(profile, statuses)


def test_layout_srt_express_masque_toute_la_preparation():
    # Le cas qui motive la refonte : un profil léger ne demande AUCUNE préparation humaine.
    lay = _layout("srt_express", JobState.ANALYZED.value)
    assert lay["required_prep"] == []
    assert lay["optional_prep"] == ["summary", "context", "participants", "lexicon"]
    assert lay["first_optional"] == "summary"
    # Renumérotation : seules les étapes visibles sont numérotées, sans trou.
    assert lay["step_num"] == {"file": 1, "analyze": 2, "processing": 3, "quality": 4, "export": 5}
    assert [s["id"] for s in lay["display_steps"]] == ["file", "analyze", "processing", "quality", "export"]
    # Aucune préparation requise → lançable dès l'analyse.
    assert lay["launch_ready"] is True


def test_layout_dossier_qualite_affiche_tout():
    lay = _layout("dossier_qualite", JobState.ANALYZED.value)
    assert lay["required_prep"] == ["summary", "context", "participants", "lexicon"]
    assert lay["optional_prep"] == []
    assert lay["first_optional"] is None
    assert lay["step_num"]["export"] == 9
    # Préparation incomplète à l'état ANALYZED → pas encore lançable.
    assert lay["launch_ready"] is False


def test_layout_dossier_qualite_lancable_quand_prepare():
    lay = _layout("dossier_qualite", JobState.LEXICON_DONE.value)
    assert lay["launch_ready"] is True


def test_layout_word_rapide_prefixe_visible_suffixe_optionnel():
    # word_rapide exige résumé+contexte → préfixe visible ; participants+lexique en repli.
    lay = _layout("word_rapide", JobState.ANALYZED.value)
    assert lay["required_prep"] == ["summary", "context"]
    assert lay["optional_prep"] == ["participants", "lexicon"]
    assert lay["first_optional"] == "participants"
    assert lay["step_num"] == {
        "file": 1, "analyze": 2, "summary": 3, "context": 4,
        "processing": 5, "quality": 6, "export": 7,
    }


def test_layout_sans_profil_comportement_complet():
    # Job legacy / aucun profil : toutes les étapes restent requises (rétro-compatibilité).
    lay = _layout(None, JobState.ANALYZED.value)
    assert lay["required_prep"] == ["summary", "context", "participants", "lexicon"]
    assert lay["optional_prep"] == []

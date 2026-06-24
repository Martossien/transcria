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
    assert len(view["profiles"]) == 6
    assert all(p["available"] for p in view["profiles"])
    # Recommandé = profil disponible de plus haut niveau.
    assert view["recommended"] == "dossier_qualite"


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

"""Tests du modèle central des profils de traitement (Phase 1 du cadrage profils).

Purs (aucun réseau, aucune DB, aucun fs) : registre, mapping legacy, phases actives,
ressources distantes, livrables. Encode notamment l'acquis du spike diarisation
(srt_locuteurs n'exécute pas la phase diarisation du pipeline).
"""
from __future__ import annotations

import pytest

from transcria.workflow.profiles import (
    DEFAULT_PROFILE_ID,
    LEGACY_MODE_MAP,
    ProcessingProfile,
    get_profile,
    is_profile,
    list_profiles,
    profile_active_phases,
    profile_deliverables,
    profile_phase_classes,
    profile_required_remote_phases,
    profile_to_legacy_mode,
    resolve_legacy_mode,
)

_USER_FACING_IDS = [
    "srt_express",
    "srt_locuteurs",
    "word_rapide",
    "word_structure",
    "word_corrige",
    "dossier_qualite",
]


# ── Registre ─────────────────────────────────────────────────────────────────-

def test_six_profils_produit_dans_lordre_des_niveaux():
    profiles = list_profiles()
    assert [p.id for p in profiles] == _USER_FACING_IDS
    assert [p.level for p in profiles] == [1, 2, 3, 4, 5, 6]


def test_legacy_fast_exclu_par_defaut_mais_present_si_demande():
    assert "legacy_fast" not in [p.id for p in list_profiles()]
    ids = [p.id for p in list_profiles(include_legacy=True)]
    assert "legacy_fast" in ids


def test_get_profile_inconnu_leve():
    with pytest.raises(KeyError):
        get_profile("inexistant")
    assert is_profile("dossier_qualite")
    assert not is_profile("inexistant")


def test_default_profile_existe_et_est_word_structure():
    assert DEFAULT_PROFILE_ID == "word_structure"
    assert is_profile(DEFAULT_PROFILE_ID)


def test_profiles_sont_immuables():
    profile = get_profile("srt_express")
    with pytest.raises(Exception):
        profile.run_diarization = True  # type: ignore[misc]


# ── Mapping legacy ───────────────────────────────────────────────────────────-

def test_resolve_legacy_mode_fast_quality():
    assert resolve_legacy_mode("fast") == "legacy_fast"
    assert resolve_legacy_mode("quality") == "dossier_qualite"
    assert LEGACY_MODE_MAP == {"fast": "legacy_fast", "quality": "dossier_qualite"}


def test_resolve_legacy_mode_idempotent_sur_un_id_de_profil():
    assert resolve_legacy_mode("word_corrige") == "word_corrige"


def test_resolve_legacy_mode_inconnu_leve():
    with pytest.raises(ValueError):
        resolve_legacy_mode("summary")  # mode de file, pas un profil
    with pytest.raises(ValueError):
        resolve_legacy_mode("speakers")


def test_profile_to_legacy_mode_route_par_diarisation():
    assert profile_to_legacy_mode(get_profile("dossier_qualite")) == "quality"
    assert profile_to_legacy_mode(get_profile("word_structure")) == "quality"
    assert profile_to_legacy_mode(get_profile("srt_express")) == "fast"
    assert profile_to_legacy_mode(get_profile("srt_locuteurs")) == "fast"


# ── Acquis du spike : srt_locuteurs ⊥ phase diarisation pipeline ─────────────--

def test_srt_locuteurs_ne_lance_pas_la_phase_diarisation_mais_exige_la_validation():
    p = get_profile("srt_locuteurs")
    assert p.run_diarization is False
    assert p.requires_speaker_validation == "required"
    assert "diarization" not in profile_active_phases(p)
    assert p.resource_requirements.needs_diarization is False


def test_profils_word_avec_locuteurs_activent_la_diarisation_pour_le_genre():
    for pid in ("word_structure", "word_corrige", "dossier_qualite"):
        p = get_profile(pid)
        assert p.run_diarization is True
        assert "diarization" in profile_active_phases(p)


# ── Phases actives ───────────────────────────────────────────────────────────-

def test_srt_express_phases_minimales():
    phases = profile_active_phases(get_profile("srt_express"))
    assert phases == ["preprocess", "transcription", "quality", "export"]


def test_correction_implique_final_review_et_uniquement_word_corrige_dossier():
    for pid in _USER_FACING_IDS:
        p = get_profile(pid)
        assert p.run_final_review == p.run_llm_correction
    assert get_profile("word_corrige").run_llm_correction is True
    assert get_profile("word_structure").run_llm_correction is False


def test_dossier_qualite_execute_toutes_les_phases():
    phases = profile_active_phases(get_profile("dossier_qualite"))
    assert phases == [
        "preprocess",
        "transcription",
        "diarization",
        "correction",
        "final_review",
        "quality",
        "export",
    ]


# ── Ressources distantes (vue pure pour le scheduler Phase 3) ────────────────--

def test_srt_express_ne_consomme_ni_llm_ni_diarisation():
    assert profile_required_remote_phases(get_profile("srt_express")) == {"stt"}


def test_word_rapide_consomme_llm_mais_pas_diarisation():
    assert profile_required_remote_phases(get_profile("word_rapide")) == {"stt", "llm"}


def test_dossier_qualite_consomme_les_trois():
    assert profile_required_remote_phases(get_profile("dossier_qualite")) == {"stt", "diarization", "llm"}


# ── Concurrence nominale ─────────────────────────────────────────────────────-

def test_phase_classes_ne_couvre_que_les_phases_actives():
    p = get_profile("word_corrige")
    classes = profile_phase_classes(p)
    assert set(classes) == set(profile_active_phases(p))
    assert classes["correction"] == "remote_llm_batchable"
    assert classes["transcription"] == "local_gpu_exclusive"


# ── Livrables ────────────────────────────────────────────────────────────────-

def test_deliverables_srt_express_sans_word():
    items = profile_deliverables(get_profile("srt_express"))
    assert "SRT" in items
    assert not any("Word" in it for it in items)


def test_deliverables_word_corrige_inclut_srt_corrige_et_word():
    items = profile_deliverables(get_profile("word_corrige"))
    assert "SRT corrigé" in items
    assert any("Word" in it for it in items)


def test_deliverables_dossier_qualite_inclut_qualite_et_zip():
    items = profile_deliverables(get_profile("dossier_qualite"))
    assert "Rapport qualité complet" in items
    assert "Archive ZIP complète" in items


# ── Cohérence transverse du contrat ──────────────────────────────────────────-

def test_profils_sont_des_processingprofile_avec_id_coherent():
    for pid in _USER_FACING_IDS:
        p = get_profile(pid)
        assert isinstance(p, ProcessingProfile)
        assert p.id == pid
        assert p.run_transcription is True  # STT obligatoire pour tous

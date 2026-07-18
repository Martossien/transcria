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
    profile_for_job,
    profile_phase_classes,
    profile_required_remote_phases,
    profile_required_steps,
    profile_required_steps_ordered,
    profile_to_legacy_mode,
    resolve_legacy_mode,
    resolve_request,
)

_USER_FACING_IDS = [
    "srt_express",
    "srt_locuteurs",
    "srt_moss",       # §4.1 — single-pass MOSS, niveau partagé avec srt_locuteurs
    "word_rapide",
    "word_structure",
    "word_corrige",
    "dossier_qualite",
]


# ── Registre ─────────────────────────────────────────────────────────────────-

def test_sept_profils_produit_dans_lordre_des_niveaux():
    profiles = list_profiles()
    assert [p.id for p in profiles] == _USER_FACING_IDS
    # srt_moss partage le niveau 2 (tri stable : srt_locuteurs d'abord, historique).
    assert [p.level for p in profiles] == [1, 2, 2, 3, 4, 5, 6]


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


# ── resolve_request (résolution requête de lancement → profil + mode routage) ─--

def test_resolve_request_profil_explicite_prioritaire():
    profile, mode = resolve_request("word_corrige", "fast")
    assert profile.id == "word_corrige"
    assert mode == "quality"  # word_corrige diarise → routage quality


def test_resolve_request_legacy_fast_et_quality():
    profile, mode = resolve_request(None, "fast")
    assert profile.id == "legacy_fast"
    assert mode == "fast"
    profile, mode = resolve_request(None, "quality")
    assert profile.id == "dossier_qualite"
    assert mode == "quality"


def test_resolve_request_defaut_sur_fast_si_rien():
    profile, mode = resolve_request(None, None)
    assert profile.id == "legacy_fast"
    assert mode == "fast"


def test_resolve_request_srt_express_route_en_fast():
    profile, mode = resolve_request("srt_express", None)
    assert profile.id == "srt_express"
    assert mode == "fast"  # pas de diarisation → routage fast


def test_resolve_request_mode_routage_derive_toujours_du_profil():
    # un id de profil passé dans le champ mode ne fuit pas comme mode d'exécution
    profile, mode = resolve_request(None, "srt_locuteurs")
    assert profile.id == "srt_locuteurs"
    assert mode in ("fast", "quality")


def test_resolve_request_invalides():
    with pytest.raises(KeyError):
        resolve_request("inexistant", None)
    with pytest.raises(ValueError):
        resolve_request(None, "summary")  # mode de file, pas un profil


# ── profile_required_steps (prérequis wizard par profil) ─────────────────────--

def test_srt_express_n_exige_aucune_etape_wizard():
    assert profile_required_steps(get_profile("srt_express")) == set()
    assert profile_required_steps_ordered(get_profile("srt_express")) == []


def test_srt_locuteurs_exige_participants():
    # Validation des locuteurs = étape « participants » du wizard.
    assert profile_required_steps(get_profile("srt_locuteurs")) == {"participants"}
    # Wizard linéaire → préfixe summary→context→participants.
    assert profile_required_steps_ordered(get_profile("srt_locuteurs")) == ["summary", "context", "participants"]


def test_word_rapide_exige_summary_context():
    assert profile_required_steps(get_profile("word_rapide")) == {"summary", "context"}
    assert profile_required_steps_ordered(get_profile("word_rapide")) == ["summary", "context"]


def test_dossier_qualite_exige_tout_dont_lexique():
    assert profile_required_steps(get_profile("dossier_qualite")) == {"summary", "context", "participants", "lexicon"}
    assert profile_required_steps_ordered(get_profile("dossier_qualite")) == [
        "summary", "context", "participants", "lexicon"
    ]


def test_word_corrige_lexique_optionnel_non_exige():
    assert "lexicon" not in profile_required_steps(get_profile("word_corrige"))


# ── profile_for_job (résolveur depuis le job persisté) ───────────────────────--

class _FakeJob:
    def __init__(self, extra):
        self._extra = extra

    def get_extra_data(self):
        return self._extra


def test_profile_for_job_lit_le_profil_persiste():
    job = _FakeJob({"execution": {"processing_profile_id": "word_corrige"}})
    assert profile_for_job(job).id == "word_corrige"


def test_profile_for_job_none_si_absent_ou_inconnu():
    assert profile_for_job(_FakeJob({})) is None
    assert profile_for_job(_FakeJob({"execution": {"processing_profile_id": "inexistant"}})) is None
    assert profile_for_job(_FakeJob({"execution": {}})) is None


# ── Cohérence transverse du contrat ──────────────────────────────────────────-

def test_profils_sont_des_processingprofile_avec_id_coherent():
    for pid in _USER_FACING_IDS:
        p = get_profile(pid)
        assert isinstance(p, ProcessingProfile)
        assert p.id == pid
        assert p.run_transcription is True  # STT obligatoire pour tous


# ── srt_moss (§4.1 : single-pass MOSS) ──────────────────────────────────────────


def test_srt_moss_phases_sans_diarisation():
    """Une passe : mêmes phases que srt_express — les locuteurs viennent du STT."""
    p = get_profile("srt_moss")
    assert profile_active_phases(p) == ["preprocess", "transcription", "quality", "export"]
    assert p.run_diarization is False
    assert p.requires_speaker_validation == "none"


def test_srt_moss_backend_impose():
    assert get_profile("srt_moss").stt_backend == "moss"


def test_tous_les_autres_profils_sans_backend_impose():
    """Garde historique : AUCUN profil existant n'impose de backend (config-driven)."""
    for pid in _USER_FACING_IDS:
        if pid != "srt_moss":
            assert get_profile(pid).stt_backend is None, pid


def test_srt_moss_ne_consomme_ni_llm_ni_diarisation():
    assert profile_required_remote_phases(get_profile("srt_moss")) == {"stt"}

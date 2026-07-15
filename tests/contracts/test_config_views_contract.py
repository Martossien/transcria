"""Contrat des vues de config (vague C3) — les défauts sont verrouillés des DEUX côtés.

Deux goldens par vue :
1. vue sur ``{}`` == défauts consommateurs (les replis que les call-sites
   historiques embarquaient — sémantique comprise) ;
2. vue sur ``get_default_config()`` == valeurs du loader.

Si le loader change un défaut, (2) casse ; si quelqu'un retouche un repli dans
``from_config``, (1) casse. Dans les deux cas la dérive est un choix conscient,
jamais un silence. S'y ajoutent les gardes de forme (frozen, sections absentes
ou scalaires tolérées — une vue ne lève JAMAIS sur une config partielle).
"""
import dataclasses

import pytest

from transcria.config.loader import get_default_config
from transcria.config.views import (
    GpuView,
    QualityTranscriptionView,
    QueueView,
    SttView,
    WorkflowView,
)

ALL_VIEWS = [GpuView, QueueView, WorkflowView, SttView, QualityTranscriptionView]


# ---------------------------------------------------------------------------
# Golden 1 — vue sur {} : les défauts de repli des consommateurs historiques
# ---------------------------------------------------------------------------

EMPTY_GOLDENS = {
    GpuView: GpuView(
        cohere_vram_mb=6000,
        pyannote_vram_mb=2000,
        llm_vram_mb=60000,
        granite_vram_mb=6000,
        voxtral_vram_mb=11000,
        moss_vram_mb=4000,
        parakeet_vram_mb=8000,
        sortformer_vram_mb=3500,
        min_free_vram_mb=4000,
        llm_gpu_indices=None,
        llm_vram_mb_per_gpu=None,
        preemption="own-only",
    ),
    QueueView: QueueView(
        enabled=True,
        default_priority=50,
        aging_enabled=True,
        aging_interval_minutes=30,
        aging_max_bonus=49,
        poll_interval_s=5,
        use_listen_notify=False,
        starvation_timeout_hours=24,
    ),
    WorkflowView: WorkflowView(
        enable_quick_summary=True,
        enable_speaker_detection=True,
        enable_quality_mode=True,
        # Absent ⇒ activée : la LLM d'arbitrage n'est coupée que par false explicite.
        arbitration_llm_enabled=True,
        # Absent ⇒ désactivé : le résumé LLM exige un true explicite.
        summary_llm_enabled=False,
        multi_stt_enabled=False,
        quality_transcription=QualityTranscriptionView(
            force_stt_backend=None,
            enabled_for_modes=(),
            force_on_degraded_summary=False,
            degraded_summary_levels=(),
        ),
    ),
    SttView: SttView(
        stt_backend="cohere",
        diarization_backend="pyannote",
        default_stt_model="",
        fallback_stt_model="",
        cohere_model_path="",
        pyannote_model="",
    ),
}


@pytest.mark.parametrize("view_cls", list(EMPTY_GOLDENS), ids=lambda v: v.__name__)
def test_vue_sur_config_vide_reproduit_les_defauts_consommateurs(view_cls):
    assert view_cls.from_config({}) == EMPTY_GOLDENS[view_cls]


# ---------------------------------------------------------------------------
# Golden 2 — vue sur la config par défaut : les valeurs du loader
# ---------------------------------------------------------------------------

DEFAULT_GOLDENS = {
    GpuView: EMPTY_GOLDENS[GpuView],  # le loader et les replis coïncident pour gpu.*
    QueueView: EMPTY_GOLDENS[QueueView],
    WorkflowView: dataclasses.replace(
        EMPTY_GOLDENS[WorkflowView],
        # Divergences CONNUES loader ↔ replis consommateurs :
        # - arbitration_llm.enabled: false dans le loader (repli permissif ⇒ true) ;
        # - multi_stt.enabled: true dans le loader (repli prudent ⇒ false) ;
        # - degraded_summary_levels: ["degrade"] dans le loader (repli vide).
        arbitration_llm_enabled=False,
        multi_stt_enabled=True,
        quality_transcription=dataclasses.replace(
            EMPTY_GOLDENS[WorkflowView].quality_transcription,
            degraded_summary_levels=("degrade",),
        ),
    ),
    SttView: dataclasses.replace(
        EMPTY_GOLDENS[SttView],
        default_stt_model="cohere-transcribe-03-2026",
        fallback_stt_model="large-v3",
        cohere_model_path="CohereLabs/cohere-transcribe-03-2026",
        pyannote_model="pyannote/speaker-diarization-community-1",
    ),
}


@pytest.mark.parametrize("view_cls", list(DEFAULT_GOLDENS), ids=lambda v: v.__name__)
def test_vue_sur_config_par_defaut_reproduit_le_loader(view_cls):
    assert view_cls.from_config(get_default_config()) == DEFAULT_GOLDENS[view_cls]


# ---------------------------------------------------------------------------
# Gardes de forme
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("view_cls", ALL_VIEWS, ids=lambda v: v.__name__)
def test_vue_figee(view_cls):
    view = view_cls.from_config({})
    field = dataclasses.fields(view)[0].name
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(view, field, "x")


@pytest.mark.parametrize("view_cls", ALL_VIEWS, ids=lambda v: v.__name__)
def test_vue_tolere_les_sections_scalaires(view_cls):
    # Une config corrompue (section = scalaire) ne fait JAMAIS lever une vue :
    # c'est le rôle du schéma de la refuser, pas celui d'une lecture.
    corrupted = {"gpu": "bad", "workflow": "bad", "models": "bad"}
    assert view_cls.from_config(corrupted) == view_cls.from_config({})


def test_llm_gpu_indices_types():
    view = GpuView.from_config({"gpu": {"llm_gpu_indices": [0, 1], "llm_vram_mb_per_gpu": [26000, 23000]}})
    assert view.llm_gpu_indices == (0, 1)
    assert view.llm_vram_mb_per_gpu == (26000, 23000)


def test_arbitration_llm_semantique_is_not_false():
    assert WorkflowView.from_config({"workflow": {"arbitration_llm": {}}}).arbitration_llm_enabled is True
    assert WorkflowView.from_config({"workflow": {"arbitration_llm": {"enabled": None}}}).arbitration_llm_enabled is True
    assert WorkflowView.from_config({"workflow": {"arbitration_llm": {"enabled": False}}}).arbitration_llm_enabled is False


def test_degraded_summary_levels_normalises():
    view = QualityTranscriptionView.from_config(
        {"workflow": {"quality_transcription": {"degraded_summary_levels": [" degrade ", "", 42, "suspect"]}}}
    )
    assert view.degraded_summary_levels == ("degrade", "42", "suspect")

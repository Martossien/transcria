"""Façade timing_service : profil → étapes → estimation (contre la vraie base)."""
from __future__ import annotations

import pytest


@pytest.fixture
def _clean(app):
    with app.app_context():
        from transcria.database import db
        from transcria.jobs.timing_store import JobTiming
        db.session.query(JobTiming).delete()
        db.session.commit()
    yield


class TestProcessingStages:
    def test_profil_lourd_vs_leger(self):
        from transcria.workflow.profiles import get_profile
        from transcria.workflow.timing_service import processing_stages
        qualite = processing_stages(get_profile("dossier_qualite"))
        assert "transcribe" in qualite and "correction" in qualite and "export" in qualite
        express = processing_stages(get_profile("srt_express"))
        assert express == ["transcribe", "export"] or "correction" not in express


class TestEstimate:
    def test_cold_start_repli_formule(self, app, _clean):
        with app.app_context():
            from transcria.workflow.profiles import get_profile
            from transcria.workflow.timing_service import estimate_processing
            e = estimate_processing(get_profile("dossier_qualite"), 600)
            assert e.basis == "initial"  # aucun historique

    def test_calibre_apres_historique(self, app, _clean):
        with app.app_context():
            from transcria.jobs.timing_store import JobTimingStore
            from transcria.workflow.profiles import get_profile
            from transcria.workflow.timing_service import estimate_processing
            prof = get_profile("dossier_qualite")
            from transcria.workflow.timing_service import processing_stages
            stages = processing_stages(prof)
            # 6 jobs historisés : chaque étape = 0.1×audio
            for audio in (120, 240, 360, 480, 600, 720):
                for st in stages:
                    JobTimingStore.record(prof.id, st, audio, 0.1 * audio)
            e = estimate_processing(prof, 600)
            assert e.basis == "measured"
            # somme des étapes : len(stages) × 0.1 × 600
            assert abs(e.seconds - len(stages) * 0.1 * 600) < 5

    def test_total_avec_humain(self, app, _clean):
        with app.app_context():
            from transcria.workflow.profiles import get_profile
            from transcria.workflow.timing_service import estimate_total_with_human
            out = estimate_total_with_human(get_profile("dossier_qualite"), 1800)
            assert out["total_minutes"] is not None
            assert out["human_minutes"] == 5           # 30 min audio → 5 min validation
            assert out["basis"] == "initial"           # pas d'historique
            assert "min" in out["machine_range"]

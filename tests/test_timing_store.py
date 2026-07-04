"""Persistance de l'historique de durées (JobTimingStore) contre la vraie base de test."""
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


class TestJobTimingStore:
    def test_record_et_relecture_ordre_chronologique(self, app, _clean):
        with app.app_context():
            from transcria.jobs.timing_store import JobTimingStore
            for a, d in [(300, 90), (600, 180), (900, 270)]:
                JobTimingStore.record("word_corrige", "correction", a, d)
            samples = JobTimingStore.recent_samples("word_corrige", "correction")
            assert samples == [(300.0, 90.0), (600.0, 180.0), (900.0, 270.0)]

    def test_valeurs_invalides_ignorees(self, app, _clean):
        with app.app_context():
            from transcria.jobs.timing_store import JobTimingStore
            JobTimingStore.record("p", "s", 0, 100)        # audio nul
            JobTimingStore.record("p", "s", 300, -5)       # durée négative
            JobTimingStore.record("p", "s", float("nan"), 5)
            JobTimingStore.record("p", "s", 300, 120)      # valide
            assert JobTimingStore.recent_samples("p", "s") == [(300.0, 120.0)]

    def test_isolation_par_profil_et_etape(self, app, _clean):
        with app.app_context():
            from transcria.jobs.timing_store import JobTimingStore
            JobTimingStore.record("A", "transcribe", 300, 60)
            JobTimingStore.record("B", "transcribe", 300, 90)
            JobTimingStore.record("A", "export", 300, 5)
            assert JobTimingStore.recent_samples("A", "transcribe") == [(300.0, 60.0)]
            assert JobTimingStore.recent_samples("B", "transcribe") == [(300.0, 90.0)]
            multi = JobTimingStore.samples_for_stages("A", ["transcribe", "export"])
            assert multi["transcribe"] == [(300.0, 60.0)] and multi["export"] == [(300.0, 5.0)]

    def test_fenetre_glissante_limite(self, app, _clean):
        with app.app_context():
            from transcria.jobs.timing_store import JobTimingStore
            for i in range(1, 8):
                JobTimingStore.record("p", "s", 100 * i, 10 * i)
            last3 = JobTimingStore.recent_samples("p", "s", limit=3)
            assert last3 == [(500.0, 50.0), (600.0, 60.0), (700.0, 70.0)]

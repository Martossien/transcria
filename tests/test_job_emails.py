"""Emails « pré-analyse prête » + « terminé » enrichi (revue macro emails)."""
from __future__ import annotations

import pytest


class TestEmailBuilders:
    # Traducteur FR source pour tester les builders (langue du destinataire, cf. Vague 3 i18n).
    @staticmethod
    def _tr():
        from transcria.notifications.mailer import _translator
        return _translator("fr", {"i18n": {"default_locale": "fr"}})

    def test_summary_ready_html_pointe_le_wizard_et_montre_les_faits(self):
        from transcria.notifications.mailer import _build_html_summary_ready
        facts = [("Type détecté", "Conseil municipal"), ("Locuteurs", "4"),
                 ("Traitement estimé", "~8 min")]
        h = _build_html_summary_ready(self._tr(), "fr", "Alice", "Réunion X", "job-1", "http://x", facts)
        assert "Pré-analyse" in h and "Conseil municipal" in h and "~8 min" in h
        assert "/jobs/job-1/wizard" in h            # l'utilisateur doit valider le contexte

    def test_completed_html_pointe_result_et_montre_temps_qualite(self):
        from transcria.notifications.mailer import _build_html_success, _build_text_success
        facts = [("Traité en", "12 min"), ("Score qualité", "93/100")]
        h = _build_html_success(self._tr(), "fr", "Alice", "Réunion X", "job-1", "http://x", facts)
        assert "/jobs/job-1/result" in h and "12 min" in h and "93/100" in h
        t = _build_text_success(self._tr(), "Alice", "Réunion X", "job-1", "http://x", facts)
        assert "Traité en : 12 min" in t and "/result" in t

    def test_faits_html_echappes(self):
        from transcria.notifications.mailer import _facts_rows_html
        rows = _facts_rows_html([("Type", "<script>alert(1)</script>")], self._tr())
        assert "<script>" not in rows and "&lt;script&gt;" in rows


class TestJobFacts:
    def test_completed_facts_lit_temps_qualite_points(self, app, tmp_path):
        with app.app_context():
            from types import SimpleNamespace
            from transcria.jobs.filesystem import JobFilesystem
            from transcria.notifications.job_facts import completed_facts
            cfg = {"storage": {"jobs_dir": str(tmp_path)}}
            fs = JobFilesystem(str(tmp_path), "j1")
            fs.save_json("quality/quality_report.json", {"quality_score": 88})
            fs.save_json("quality/review_points.json", ["p1", "p2"])
            facts = completed_facts(cfg, SimpleNamespace(id="j1"), processing_seconds=725)
            d = dict(facts)
            assert d["Traité en"] == "12 min" and d["Score qualité"] == "88/100"
            assert d["Points à vérifier"] == "2"

    def test_summary_ready_facts_type_et_estimation(self, app, tmp_path, _clean):
        with app.app_context():
            from types import SimpleNamespace
            from transcria.jobs.filesystem import JobFilesystem
            from transcria.notifications.job_facts import summary_ready_facts
            cfg = {"storage": {"jobs_dir": str(tmp_path)}}
            fs = JobFilesystem(str(tmp_path), "j2")
            fs.save_json("metadata/audio_analysis.json", {"duration_seconds": 1800})
            fs.save_json("context/meeting_context.json",
                         {"meeting_type": "Conseil", "participants": [{"n": 1}, {"n": 2}]})
            job = SimpleNamespace(id="j2",
                                  get_extra_data=lambda: {"execution": {"processing_profile_id": "dossier_qualite"}})
            facts = dict(summary_ready_facts(cfg, job))
            assert facts["Type détecté"] == "Conseil" and facts["Locuteurs"] == "2"
            assert "Durée audio" in facts and "Traitement estimé" in facts


@pytest.fixture
def _clean(app):
    with app.app_context():
        from transcria.database import db
        from transcria.jobs.timing_store import JobTiming
        db.session.query(JobTiming).delete()
        db.session.commit()
    yield

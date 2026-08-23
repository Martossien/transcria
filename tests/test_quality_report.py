

class TestInconsistentWordForms:
    """« Signaler sans corriger » : formes incohérentes HORS glossaire remontées à
    l'humain (jamais corrigées automatiquement — périmètre de la relecture finale)."""

    SRT = ("1\n00:00:01,000 --> 00:00:03,000\nMettez-moi un peu d'émental s'il vous plaît.\n\n"
           "2\n00:00:04,000 --> 00:00:06,000\nL'emental est en promotion.\n\n"
           "3\n00:00:07,000 --> 00:00:09,000\nLe Fromage est bon. Ce fromage est doux.\n")

    def test_detecte_accent_hors_glossaire(self):
        from transcria.quality.quality_report import QualityReporter
        found = QualityReporter._find_inconsistent_word_forms(self.SRT, [])
        forms = {tuple(sorted(g["forms"])) for g in found}
        assert ("emental", "émental") in forms

    def test_casse_pure_non_signalee(self):
        from transcria.quality.quality_report import QualityReporter
        found = QualityReporter._find_inconsistent_word_forms(self.SRT, [])
        assert all("fromage" not in g["forms"] for g in found)  # Fromage/fromage = début de phrase

    def test_terme_du_glossaire_exclu(self):
        from transcria.quality.quality_report import QualityReporter
        lexicon = [{"term": "Emmental", "variants": ["émental", "emental"]}]
        found = QualityReporter._find_inconsistent_word_forms(self.SRT, lexicon)
        assert all("emental" not in g["forms"] for g in found)

    def test_ancres_de_recherche_pour_l_editeur(self):
        from transcria.quality.review_points import ReviewPoints
        report = {"checks": [{"type": "inconsistent_word_forms", "count": 1,
                              "groups": [{"forms": ["émental", "emental"], "occurrences": 2}]}]}
        points = ReviewPoints.generate(report)
        anchors = ReviewPoints.generate_anchors(report)
        assert any("signalées sans correction" in p for p in points)
        assert anchors and anchors[0]["kind"] == "search" and anchors[0]["query"] == "emental"


class TestPassesLlmMuettesDansLeRapportComplet:
    """T5 (2026-08-23) : les contrôles « passe LLM muette » ne vivaient que dans le
    rapport LÉGER. La relecture finale morte d'un job en qualité complète (gel amont,
    0/4 sorties) n'a levé aucun point à vérifier. Mêmes contrôles ici, mêmes gardes."""

    def _report(self, tmp_path, meeting_ctx=None):

        from transcria.jobs.filesystem import JobFilesystem
        from transcria.jobs.models import Job, JobState
        from transcria.quality.quality_report import QualityReporter

        fs = JobFilesystem(str(tmp_path), "j-muette")
        srt = "1\n00:00:01,000 --> 00:00:04,000\nBonjour tout le monde\n"
        fs.save_text("metadata/transcription.srt", srt)
        fs.save_text("metadata/transcription_corrigee.srt", srt)
        fs.save_json("metadata/transcription_segments.json",
                     [{"start": 1.0, "end": 4.0, "text": "Bonjour tout le monde"}])
        if meeting_ctx is not None:
            fs.save_json("context/meeting_context.json", meeting_ctx)
        job = Job(id="j-muette", owner_id="u1", title="T", state=JobState.CREATED.value)
        return QualityReporter({"storage": {"jobs_dir": str(tmp_path)}}).run_all_checks(job)

    def test_la_relecture_morte_leve_un_point_dans_le_rapport_complet(self, tmp_path):
        report = self._report(tmp_path, {"summary_llm": "## Synthèse\n\nTexte."})

        types = {c["type"] for c in report["checks"]}
        assert "silent_final_review" in types
        assert "silent_correction" in types  # corrigée == source, même signal

    def test_sans_synthese_amont_rien_n_est_leve_pour_la_relecture(self, tmp_path):
        report = self._report(tmp_path)

        assert not any(c["type"] == "silent_final_review" for c in report["checks"])

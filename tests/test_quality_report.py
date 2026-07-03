

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

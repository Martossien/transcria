"""Registre de sections DOCX ordonnées (lot C — docs/TYPES_REUNION_PERSONNALISES.md §5.1).

Le test de NON-RÉGRESSION ci-dessous a été écrit AVANT le refactor de ``build()``
(point P2 du cadrage) : l'ordre par défaut, les libellés et la numérotation
séquentielle sont l'instantané du comportement historique — ils ne bougent JAMAIS
sans décision explicite. Les ordres personnalisés (fiche de type / render_options)
sont testés par-dessus.
"""
import re

import pytest

pytest.importorskip("docx")

from transcria.exports.docx_report import DocxReport  # noqa: E402


def _full_ctx(**overrides) -> dict:
    ctx = {
        "title": "Réunion de synthèse",
        "date": "2026-07-03",
        "meeting_type": "CSE",
        "topic": "Sujet principal",
        "objective": "Objectif de la séance",
        "notes": "Notes libres",
        "summary": "## Synthèse\nPoints saillants de la réunion.",
        "type_specific_data": {"president_seance": "A. Dupont", "membres_presents": 6, "membres_total": 10},
    }
    ctx.update(overrides)
    return ctx


_STRUCTURED = {
    "points_odj": ["1. Budget — vue d'ensemble"],
    "decisions": ["Décision X actée"],
    "votes": ["Budget : 6 pour, 2 contre — adopté"],
    "resolutions": ["Résolution n°1 adoptée"],
    "actions": ["R. Martin : préparer le dossier"],
    "blocages": ["Blocage fournisseur"],
    "reports": ["Point RH reporté"],
}

_PARTICIPANTS = [{"id": "p1", "name": "A. Dupont", "function": "Présidente", "role": "animatrice"}]
_SPEAKERS = {"speakers": [{"speaker_id": "SPEAKER_00", "speaking_time_seconds": 60, "turn_count": 3}]}
_QUALITY = {"quality_score": 88,
            "checks": [{"type": "empty_segments", "severity": "warning", "count": 2}]}
_SRT = "1\n00:00:00,000 --> 00:00:02,000\nBonjour à tous.\n\n2\n00:00:02,000 --> 00:00:04,000\nOuvrons la séance.\n"

# Instantané du comportement HISTORIQUE (avant le registre) — ordre + libellés + numéros.
DEFAULT_HEADINGS = [
    "1. CONTEXTE DE LA RÉUNION",
    "2. ORDRE DU JOUR",
    "3. DÉCISIONS PRISES",
    "4. VOTES",
    "5. RÉSOLUTIONS ADOPTÉES",
    "6. ACTIONS À RÉALISER",
    "7. POINTS BLOQUANTS",
    "8. POINTS REPORTÉS",
    "9. PARTICIPANTS & LOCUTEURS",
    "10. TRANSCRIPTION",
    "11. POINTS À VÉRIFIER",
]


def _build(ctx=None, render_options=None):
    report = DocxReport(ctx or _full_ctx(), _PARTICIPANTS, _SPEAKERS, _QUALITY, _SRT,
                        structured_data=dict(_STRUCTURED), render_options=render_options)
    return report.build()


def _headings(doc) -> list[str]:
    found = []
    for p in doc.paragraphs:
        text = " ".join(p.text.split())
        if re.match(r"^\d+\. \S", text):
            found.append(text)
    return found


class TestNonRegressionOrdreParDefaut:
    def test_ordre_libelles_et_numeros_historiques(self):
        assert _headings(_build()) == DEFAULT_HEADINGS

    def test_sections_desactivees_renumerotees(self):
        doc = _build(render_options={"sections": {"transcript": False, "quality": False}})
        assert _headings(doc) == DEFAULT_HEADINGS[:9]

    def test_pv_vide_silencieux(self):
        report = DocxReport(_full_ctx(), _PARTICIPANTS, _SPEAKERS, _QUALITY, _SRT,
                            structured_data={})
        heads = _headings(report.build())
        assert heads[0] == "1. CONTEXTE DE LA RÉUNION"
        assert heads[1] == "2. PARTICIPANTS & LOCUTEURS"


def _custom_ctx(sections=None, branding=None) -> dict:
    ctx = _full_ctx(meeting_type="COMEX Société X")
    ctx["custom_type"] = {
        "name": "COMEX Société X",
        "badge": "COMEX",
        "banner_text": "COMPTE-RENDU — COMITÉ EXÉCUTIF",
        "palette": {"primary": "1C1C1C", "accent": "424242", "light": "F5F5F5"},
        "behavior": {"quorum": False, "confidential": False},
        "fields": [{"key": "filiale", "label": "Filiale concernée", "short_label": "Filiale", "type": "text"}],
        "sections": sections or {},
        "branding": branding or {},
        "template_id": "x",
    }
    ctx["type_specific_data"] = {"filiale": "Filiale Nord"}
    return ctx


class TestOrdresPersonnalises:
    def test_synthese_executive_en_premier(self):
        # Le cas utilisateur fondateur : « d'abord un résumé exécutif, ensuite X, Y, Z ».
        ctx = _custom_ctx(sections={"order": ["synthese", "contexte", "pv", "participants", "transcript", "quality"]})
        heads = _headings(_build(ctx))
        assert heads[0] == "1. SYNTHÈSE"
        assert heads[1] == "2. CONTEXTE DE LA RÉUNION"
        assert heads[2] == "3. ORDRE DU JOUR"          # blocs PV renumérotés à la suite
        assert heads[-1] == "12. POINTS À VÉRIFIER"

    def test_ordre_de_la_fiche_surcharge_par_le_job(self):
        # render_options (chat d'affinage) PRIME sur les défauts de la fiche.
        ctx = _custom_ctx(sections={"order": ["synthese", "contexte", "pv"]})
        doc = _build(ctx, render_options={"order": ["contexte", "pv"]})
        heads = _headings(doc)
        assert heads[0] == "1. CONTEXTE DE LA RÉUNION"
        assert "SYNTHÈSE" not in " ".join(heads)  # plus de section autonome

    def test_unites_obligatoires_reinjectees(self):
        # Un ordre qui « oublie » contexte/pv ne les supprime pas : déplaçables, jamais
        # supprimables (règle : une donnée extraite n'est jamais cachée).
        ctx = _custom_ctx(sections={"order": ["participants"]})
        heads = _headings(_build(ctx))
        assert heads[0] == "1. PARTICIPANTS & LOCUTEURS"
        assert "2. CONTEXTE DE LA RÉUNION" in heads
        assert any("ORDRE DU JOUR" in h for h in heads)

    def test_champs_type_en_section_autonome(self):
        ctx = _custom_ctx(sections={"order": ["contexte", "champs_type", "pv", "participants", "transcript", "quality"]})
        heads = _headings(_build(ctx))
        assert heads[1] == "2. INFORMATIONS SPÉCIFIQUES"

    def test_sections_enabled_de_la_fiche_par_defaut(self):
        # La fiche désactive la transcription ; le job ne dit rien → défaut de la fiche.
        ctx = _custom_ctx(sections={"enabled": {"transcript": False}})
        heads = _headings(_build(ctx))
        assert not any("TRANSCRIPTION" in h for h in heads)
        # …mais le job peut la réactiver (surcharge par render_options).
        heads2 = _headings(_build(ctx, render_options={"sections": {"transcript": True}}))
        assert any("TRANSCRIPTION" in h for h in heads2)

    def test_synthese_absente_pas_de_section_vide(self):
        ctx = _custom_ctx(sections={"order": ["synthese", "contexte", "pv"]})
        ctx["summary"] = ""
        heads = _headings(_build(ctx))
        assert heads[0] == "1. CONTEXTE DE LA RÉUNION"  # pas de « 1. SYNTHÈSE » vide


class TestBranding:
    def test_pied_de_page_de_la_fiche(self):
        ctx = _custom_ctx(branding={"footer_text": "Société X — diffusion restreinte"})
        doc = _build(ctx)
        footer_text = "\n".join(p.text for p in doc.sections[0].footer.paragraphs)
        assert "Société X — diffusion restreinte" in footer_text

    def test_logo_insere_en_couverture(self):
        import io

        from PIL import Image

        from transcria.exports.docx_report import DocxReport
        buf = io.BytesIO()
        Image.new("RGBA", (120, 40), (30, 30, 30, 255)).save(buf, format="PNG")
        report = DocxReport(_custom_ctx(), _PARTICIPANTS, _SPEAKERS, _QUALITY, _SRT,
                            structured_data=dict(_STRUCTURED), logo_bytes=buf.getvalue())
        doc = report.build()
        assert len(doc.inline_shapes) == 1

    def test_logo_corrompu_ne_plante_pas(self):
        from transcria.exports.docx_report import DocxReport
        report = DocxReport(_custom_ctx(), _PARTICIPANTS, _SPEAKERS, _QUALITY, _SRT,
                            structured_data=dict(_STRUCTURED), logo_bytes=b"pas une image")
        doc = report.build()
        assert len(doc.inline_shapes) == 0


class TestExtractionsPersonnalisees:
    def test_bloc_extraction_rendu_apres_les_blocs_pv(self):
        # Lot D : les clés d'extraction de la fiche s'affichent comme les blocs PV.
        ctx = _custom_ctx()
        ctx["custom_type"]["extract_fields"] = [
            {"key": "budgets_evoques", "label": "Budgets évoqués",
             "instruction": "montants budgétaires explicitement cités"},
        ]
        sd = dict(_STRUCTURED)
        sd["budgets_evoques"] = ["10 k€ pour le projet A"]
        from transcria.exports.docx_report import DocxReport
        doc = DocxReport(ctx, _PARTICIPANTS, _SPEAKERS, _QUALITY, _SRT, structured_data=sd).build()
        heads = _headings(doc)
        assert "9. BUDGETS ÉVOQUÉS" in heads   # après les 7 blocs PV, avant Participants
        assert heads[heads.index("9. BUDGETS ÉVOQUÉS") + 1] == "10. PARTICIPANTS & LOCUTEURS"

    def test_cle_absente_du_structured_silencieuse(self):
        ctx = _custom_ctx()
        ctx["custom_type"]["extract_fields"] = [
            {"key": "budgets_evoques", "label": "Budgets évoqués", "instruction": "x"},
        ]
        from transcria.exports.docx_report import DocxReport
        doc = DocxReport(ctx, _PARTICIPANTS, _SPEAKERS, _QUALITY, _SRT,
                         structured_data=dict(_STRUCTURED)).build()
        assert not any("BUDGETS" in h for h in _headings(doc))

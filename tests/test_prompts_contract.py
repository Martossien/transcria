"""Contrats prompt ↔ code — la CI garde les deux synchrones.

Les trois prompts (`configs/prompts/`) sont le fruit de très nombreux tests : on ne
vérifie PAS leur contenu rédactionnel, seulement les invariants que le code consomme
(noms de sections parsées, noms de fichiers de sortie, liste des types de réunion,
champs du JSON structuré) et les régressions de forme déjà rencontrées (chemin hors
répertoire de travail, alternative vide dans un grep).
"""
from __future__ import annotations

from pathlib import Path

_PROMPTS = Path(__file__).resolve().parents[1] / "configs" / "prompts"


def _read(name: str) -> str:
    return (_PROMPTS / name).read_text(encoding="utf-8")


class TestSummaryPromptContract:
    def test_les_18_types_de_reunion_du_code_sont_dans_le_prompt(self):
        """La liste des types est dupliquée prompt/code : toute dérive est silencieuse
        (le type suggéré par la LLM ne matcherait plus le menu du wizard ni les thèmes
        DOCX)."""
        from transcria.context.meeting_context import MEETING_TYPES

        text = _read("summary_prompt.txt")
        for mtype in MEETING_TYPES:
            assert mtype in text, f"type absent du prompt résumé : {mtype}"

    def test_sections_parsees_par_le_code_presentes(self):
        """`_parse_structured_summary` lit ces titres au caractère près."""
        text = _read("summary_prompt.txt")
        for heading in (
            "## Informations sur la réunion",
            "## Participants probables",
            "## Synthèse",
            "## Termes douteux à valider",
            "## Données structurées",
        ):
            assert heading in text, f"section parsée absente du prompt : {heading}"

    def test_champs_structured_data_alignes_sur_le_normaliseur(self):
        """Les champs extraits doivent exister dans `_normalize_structured_data`
        (et donc dans le DOCX/UI) — un champ renommé serait jeté en silence."""
        text = _read("summary_prompt.txt")
        for field in ("decisions", "actions", "blocages", "reports",
                      "votes", "resolutions", "points_odj", "prochaine_date"):
            assert f'"{field}"' in text, f"champ structured_data absent du prompt : {field}"

    def test_fichier_de_sortie(self):
        assert "summary.md" in _read("summary_prompt.txt")

    def test_grep_fixe_sans_alternative_vide(self):
        """Régression v2.9 : le pattern se terminait par `\\|` (alternative vide) et
        matchait TOUTES les lignes — le grep de re-ancrage était un `cat` déguisé."""
        text = _read("summary_prompt.txt")
        assert '\\|"' not in text


class TestCorrectionPromptContract:
    def test_fichiers_de_sortie(self):
        text = _read("correction_prompt.txt")
        assert "transcription_corrigee.srt" in text
        assert "correction_report.md" in text

    def test_aucun_chemin_hors_repertoire_de_travail(self):
        """Régression v2.3 : l'agent tourne isolé dans un scratch (AgentWorkspace) —
        tout chemin relatif `../` du prompt pointe dans le vide depuis ce cwd."""
        assert "../" not in _read("correction_prompt.txt")

    def test_prefixe_locuteur_explicitement_intouchable(self):
        """Régression v2.3 : la formulation abstraite (« ne pas corriger les
        locuteurs ») a été interprétée librement par un modèle plus faible
        (préfixes réécrits en `Nom:`) — la forme exacte doit être nommée."""
        assert "SPEAKER_XX(Nom):" in _read("correction_prompt.txt")

    def test_ratio_anti_resume_aligne_sur_la_garde_code(self):
        """Le prompt exige 0.90–1.10 ; `_corrected_srt_integrity_error` vérifie pareil."""
        text = _read("correction_prompt.txt")
        assert "0.90" in text and "1.10" in text


class TestFinalReviewPromptContract:
    def test_les_4_fichiers_de_sortie(self):
        """`OpenCodeRunner.run_final_review` lit exactement ces noms dans le scratch."""
        text = _read("final_review_prompt.txt")
        for name in ("summary_harmonized.md", "transcription_reviewed.srt",
                     "structured_data_reviewed.json", "final_review_report.md"):
            assert name in text, f"fichier de sortie absent du prompt : {name}"

    def test_prefixe_locuteur_intouchable(self):
        assert "SPEAKER_XX" in _read("final_review_prompt.txt")


class TestRefinePromptsContract:
    """Chat d'affinage : les noms de fichiers de sortie et le vocabulaire des options
    de rendu doivent rester alignés sur ``run_refine`` / ``_apply_refine``."""

    def test_discuss_fichier_de_sortie(self):
        assert "refine_answer.md" in _read("refine_discuss_prompt.txt")

    def test_discuss_lecture_seule(self):
        p = _read("refine_discuss_prompt.txt")
        assert "NE MODIFIER AUCUN FICHIER" in p

    def test_discuss_label_proposition_aligne_sur_extracteur(self):
        # extract_proposal() parse ce label littéral — le prompt doit l'imposer tel quel.
        p = _read("refine_discuss_prompt.txt")
        assert "Proposition d'application :" in p
        assert "aucune" in p

    def test_apply_fichiers_de_sortie(self):
        p = _read("refine_apply_prompt.txt")
        for name in ("summary_refined.md", "transcription_refined.srt",
                     "structured_data_refined.json", "render_options_refined.json",
                     "refine_report.md"):
            assert name in p, name

    def test_apply_prefixe_locuteur_intouchable(self):
        assert "SPEAKER_XX" in _read("refine_apply_prompt.txt")

    def test_apply_sections_de_rendu_alignees_sur_le_code(self):
        from transcria.exports.docx_report import _RENDER_SECTIONS

        p = _read("refine_apply_prompt.txt")
        for key in _RENDER_SECTIONS:
            assert f"`{key}`" in p, key

    def test_aucun_extrait_reel_de_transcription(self):
        # Contrainte projet : placeholders abstraits uniquement dans les templates.
        for name in ("refine_discuss_prompt.txt", "refine_apply_prompt.txt"):
            p = _read(name)
            assert "SPEAKER_00(" not in p and "SPEAKER_01(" not in p, name

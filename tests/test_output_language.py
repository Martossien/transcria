"""Axe B (Vague 4) : langue des livrables — résolution de langue, prompt localisé, consigne.

Couvre le PLUMBING pur (GPU-free). La qualité de rédaction EN et le parsing bout-en-bout
relèvent d'une passe E2E GPU (bêta), hors périmètre de ces tests unitaires.
"""
from __future__ import annotations

from pathlib import Path

from transcria.gpu.opencode_runner import (
    language_directive,
    resolve_output_language,
    resolve_prompt_file,
)


def test_resolve_output_language_defaults_to_fr():
    assert resolve_output_language(extra_data={}) == "fr"
    assert resolve_output_language(extra_data={"meeting_context": {}}) == "fr"


def test_resolve_output_language_reads_meeting_context():
    assert resolve_output_language(extra_data={"meeting_context": {"language": "en"}}) == "en"
    assert resolve_output_language(extra_data={"meeting_context": {"language": "de"}}) == "de"


def test_resolve_output_language_from_job_object():
    class _Job:
        def get_extra_data(self):
            return {"meeting_context": {"language": "es"}}
    assert resolve_output_language(job=_Job()) == "es"


def test_resolve_prompt_file_fr_uses_root(tmp_path: Path):
    base = tmp_path / "prompts"
    base.mkdir()
    (base / "summary_prompt.txt").write_text("FR", encoding="utf-8")
    cfg = {"workflow": {"prompts_dir": str(base)}}
    assert resolve_prompt_file(cfg, "summary_prompt.txt", "fr") == str((base / "summary_prompt.txt").resolve())


def test_resolve_prompt_file_en_prefers_localized(tmp_path: Path):
    base = tmp_path / "prompts"
    (base / "en").mkdir(parents=True)
    (base / "summary_prompt.txt").write_text("FR", encoding="utf-8")
    (base / "en" / "summary_prompt.txt").write_text("EN", encoding="utf-8")
    cfg = {"workflow": {"prompts_dir": str(base)}}
    resolved = resolve_prompt_file(cfg, "summary_prompt.txt", "en")
    assert resolved == str((base / "en" / "summary_prompt.txt").resolve())
    assert Path(resolved).read_text(encoding="utf-8") == "EN"


def test_resolve_prompt_file_en_falls_back_to_fr_when_missing(tmp_path: Path):
    base = tmp_path / "prompts"
    base.mkdir()
    (base / "summary_prompt.txt").write_text("FR", encoding="utf-8")
    cfg = {"workflow": {"prompts_dir": str(base)}}
    # pas de en/ → repli sur la racine (français source) : non-régression garantie
    assert resolve_prompt_file(cfg, "summary_prompt.txt", "en") == str((base / "summary_prompt.txt").resolve())


def test_language_directive_empty_for_fr():
    assert language_directive("fr") == ""
    assert language_directive("") == ""


def test_language_directive_mentions_target():
    d = language_directive("en")
    assert "English" in d and "marqueurs de format" in d


# ── Parsers pilotés par la langue (table de marqueurs) ────────────────────────

_FR_SUMMARY = """## Synthèse
Texte de synthèse d'exemple (placeholder, aucun extrait réel).

**Titre suggéré :** Titre Alpha
**Type suggéré :** CSE
**Sujet principal :** Sujet Beta
**Objectif probable :** Objectif Gamma
**Notes / Ordre du jour probable :** Note Delta
**Mots-clés**
mot1, mot2
**Nombre de participants détectés :** 2

## Participants probables
- SPEAKER_00 [Alpha] : présidence
- SPEAKER_01 : Beta — rapporteur

## Termes suspects/douteux
| terme | variantes | catégorie | priorité |
| Epsilon | epslon | mot suspect | normale |

## Données structurées
```json
{"decisions": ["D1"], "actions": ["A1"]}
```
"""


def _to_english(md: str) -> str:
    from transcria.gpu.opencode_runner import summary_markers
    fr, en = summary_markers("fr"), summary_markers("en")
    for key in ("title", "type", "subject", "objective", "notes", "keywords",
                "participant_count", "participants_heading", "summary_heading"):
        md = md.replace(fr[key], en[key])
    md = md.replace("## Termes suspects/douteux", "## Doubtful terms")
    md = md.replace("## Données structurées", "## Structured data")
    return md


def test_summary_parser_fr_unchanged():
    """Non-régression : le résumé français parse comme avant (défaut fr)."""
    from transcria.gpu.opencode_runner import OpenCodeRunner
    p = OpenCodeRunner._parse_structured_summary(_FR_SUMMARY)  # défaut language="fr"
    assert p["title_suggere"] == "Titre Alpha"
    assert p["type_suggere"] == "CSE"
    assert p["sujet_suggere"] == "Sujet Beta"
    assert p["objectif_suggere"] == "Objectif Gamma"
    assert p["speaker_count"] == 2
    assert "SPEAKER_00" in p["participants_detectes"] and "SPEAKER_01" in p["participants_detectes"]
    assert [t["term"] for t in p["termes_suspects"]] == ["Epsilon"]
    assert p["structured_data"]["decisions"] == ["D1"]
    assert p["structured_data"]["actions"] == ["A1"]


def test_summary_parser_en_markers_extract_same_fields():
    """Le même contenu avec marqueurs anglais parse identiquement en mode en."""
    from transcria.gpu.opencode_runner import OpenCodeRunner
    en_md = _to_english(_FR_SUMMARY)
    p = OpenCodeRunner._parse_structured_summary(en_md, (), "en")
    assert p["title_suggere"] == "Titre Alpha"      # les VALEURS ne changent pas dans ce test
    assert p["speaker_count"] == 2
    assert "SPEAKER_00" in p["participants_detectes"]
    assert [t["term"] for t in p["termes_suspects"]] == ["Epsilon"]
    assert p["structured_data"]["decisions"] == ["D1"]


def test_en_markers_not_found_in_fr_mode():
    """Garde-fou : les marqueurs EN ne sont PAS lus en mode fr (isolation des chemins)."""
    from transcria.gpu.opencode_runner import OpenCodeRunner
    en_md = _to_english(_FR_SUMMARY)
    p_fr = OpenCodeRunner._parse_structured_summary(en_md, (), "fr")  # marqueurs EN, parser FR
    assert p_fr["title_suggere"] == ""              # le parser FR ne trouve pas "Suggested title"
    assert p_fr["speaker_count"] == 0


def test_quality_reports_localized():
    """Rapports qualité (léger + complet) : markdown généré dans la langue du livrable."""
    from transcria.quality.light_report import _format_markdown as light_md
    from transcria.quality.light_report import _strings as light_strings
    from transcria.quality.quality_report import QualityReporter, _qr_strings
    # Léger
    rep = {"quality_score": 88, "total_checks": 4, "warnings": 1,
           "review_points": ["Empty segments: 1 — check and remove manually."]}
    md_en = light_md(rep, light_strings("en"))
    assert md_en.startswith("# Quality report (light check)")
    assert "## Points to review" in md_en and "Quality score: 88/100" in md_en
    md_fr = light_md(rep, light_strings("fr"))
    assert md_fr.startswith("# Rapport qualité (contrôle léger)")
    # Complet
    qr = QualityReporter(config={})
    qr.S = _qr_strings("en")
    full_en = qr._format_markdown({"quality_score": 77, "total_checks": 6, "warnings": 2,
                                   "review_points": [], "checks": [], "review_load": {}})
    assert "# Quality report" in full_en and "## Points to review" in full_en
    assert "No point of attention detected" in full_en
    # fr par défaut (self.S absent) = repli
    qr2 = QualityReporter(config={})
    assert qr2._format_markdown({"quality_score": 50, "total_checks": 1, "warnings": 0,
                                 "review_points": [], "checks": [], "review_load": {}}).startswith("# Rapport qualité")


def test_localized_field_labels():
    """Libellés des champs type-spécifiques localisés (repli fr/authoré)."""
    from transcria.context.meeting_type_catalog import localized_field_labels
    assert localized_field_labels("en")["formateur"] == "Trainer"
    assert localized_field_labels("en")["nom_client"] == "Client"
    assert localized_field_labels("fr")["formateur"] == "Formateur"  # fr inchangé


def test_refine_messages_by_language():
    """Messages du chat d'affinage localisés (repli fr)."""
    from transcria.workflow.runner import _refine_messages
    assert _refine_messages("en")["progress_done"] == "Refinement complete"
    assert _refine_messages("fr")["progress_done"] == "Affinage terminé"
    assert _refine_messages("de")["busy"].startswith("L'assistant")  # repli fr
    assert "{exc}" in _refine_messages("en")["fail"]


def test_proposal_label_bilingual():
    """extract_proposal reconnaît le label EN « Apply proposal: » et « none »."""
    from transcria.workflow.refine_store import extract_proposal
    text_en = "Here is my answer.\n\n---\nApply proposal: shorten the summary"
    body, prop = extract_proposal(text_en)
    assert prop == "shorten the summary" and "Apply proposal" not in body
    _, none = extract_proposal("Answer.\n\n---\nApply proposal: none")
    assert none is None


def test_docx_type_specific_field_localized_en():
    """Rendu DOCX EN : les libellés de champs type-spécifiques sont en anglais."""
    from transcria.exports.docx_report import DocxReport
    ctx = {"language": "en", "meeting_type": "Formation",
           "type_specific_data": {"formateur": "Acme Corp", "nb_participants_formation": "12"}}
    rep = DocxReport(ctx=ctx, participants=[], speaker_stats={}, quality={}, srt_text="")
    doc = rep.build()
    full = "\n".join(p.text for p in doc.paragraphs)
    for t in doc.tables:
        for row in t.rows:
            full += "\n" + " | ".join(c.text for c in row.cells)
    assert "Trainer" in full and "Participants" in full
    assert "Formateur" not in full and "Nb participants" not in full


def test_localized_type_display():
    """Affichage du type localisé (clé FR conservée pour la logique ; repli custom)."""
    from transcria.context.meeting_type_catalog import localized_type_display as f
    assert f("Podcast / média", "en", "name", "Podcast / média") == "Podcast / media"
    assert f("Podcast / média", "en", "badge", "MÉDIA") == "MEDIA"
    assert f("Réunion de crise", "en", "name", "Réunion de crise") == "Crisis meeting"
    assert f("Réunion interne", "fr", "name", "Réunion interne") == "Réunion interne"  # fr inchangé
    assert f("Type Custom Utilisateur", "en", "name", "Type Custom Utilisateur") == "Type Custom Utilisateur"  # repli


def test_docx_quality_labels_localized():
    """Les libellés + descriptions des points à vérifier sont localisés (pas de FR en EN)."""
    from transcria.exports.docx_report import _docx_labels
    en = _docx_labels("en")
    assert en["q_coverage"] == "⚠  Audio coverage"
    assert en["badge_crise"] == "⚠  CRISIS SITUATION  ⚠"
    assert en["chk_empty_segments"] == "Empty segments"
    # les templates de description se formatent proprement
    assert en["d_coverage"].format(pct=63) == "63% — possible transcription loss"
    assert en["d_altered_name"].format(sid="S0", found="X", expected="Y") == "S0: “X” instead of “Y”"


def test_docx_labels_by_language():
    """Table de libellés DOCX : en localisé, fr/inconnu = français (repli)."""
    from transcria.exports.docx_report import _docx_labels
    assert _docx_labels("en")["banner"] == "TRANSCRIPTION REPORT"
    assert _docx_labels("en")["sec_participants"] == "Participants & Speakers"
    assert _docx_labels("fr")["banner"] == "COMPTE-RENDU DE TRANSCRIPTION"
    assert _docx_labels("de")["banner"] == "COMPTE-RENDU DE TRANSCRIPTION"  # repli fr


def test_docx_extract_synthese_en_ignores_meta_and_json():
    """Bug corrigé : en EN, on extrait la seule prose (pas les méta ni le bloc JSON).

    Structure réaliste d'un summary.md : les méta et le JSON encadrent la section
    synthèse ; ``_extract_synthese`` ne doit renvoyer QUE la prose de cette section."""
    from transcria.exports.docx_report import _extract_synthese
    en_md = (
        "# Summary report\n\n## Meeting information\n- **Suggested title:** X\n\n"
        "## Summary\nThe speaker opens with a vision. A second paragraph follows.\n\n"
        "## Doubtful terms to validate\n- **term** [x] (normal)\n\n"
        "## Structured data\n```json\n{\"decisions\": []}\n```\n"
    )
    prose_en = _extract_synthese(en_md, "en")
    assert prose_en.startswith("The speaker opens with a vision")
    for banned in ("Suggested title", "Structured data", '"decisions"', "```json", "Doubtful terms"):
        assert banned not in prose_en, f"fuite dans la prose EN : {banned}"
    # FR non-régression : même extraction depuis « ## Synthèse ».
    fr_md = "## Synthèse\nProse française.\n\n## Données structurées\n```json\n{}\n```\n"
    prose_fr = _extract_synthese(fr_md, "fr")
    assert prose_fr == "Prose française."


def test_effective_summary_marker_by_language():
    """La section synthèse remplacée dépend de la langue du meeting."""
    from transcria.context.meeting_context import MeetingContextManager
    raw_fr = "## Synthèse\nancien texte\n## Autre\nx"
    out = MeetingContextManager.effective_summary_markdown({"summary": "NOUVEAU", "language": "fr"}, raw_fr)
    assert "NOUVEAU" in out and "## Synthèse" in out
    raw_en = "## Summary\nold text\n## Other\nx"
    out_en = MeetingContextManager.effective_summary_markdown({"summary": "NEW", "language": "en"}, raw_en)
    assert "NEW" in out_en and "## Summary" in out_en

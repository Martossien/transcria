"""Génère un rapport DOCX professionnel à partir des artefacts d'un job terminé."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from transcria.jobs.filesystem import JobFilesystem

# ── Palette ──────────────────────────────────────────────────────────────────
_BLUE_DARK  = RGBColor(0x1F, 0x38, 0x64)
_BLUE_MID   = RGBColor(0x2E, 0x74, 0xB5)
_BLUE_LIGHT = RGBColor(0xD6, 0xE4, 0xF0)
_GREY_DARK  = RGBColor(0x59, 0x59, 0x59)
_GREY_LIGHT = RGBColor(0xF5, 0xF5, 0xF5)
_RED        = RGBColor(0xC0, 0x00, 0x00)
_GREEN      = RGBColor(0x37, 0x86, 0x47)
_ORANGE     = RGBColor(0xED, 0x7D, 0x31)
_YELLOW_BG  = RGBColor(0xFF, 0xFF, 0xCC)
_WHITE      = RGBColor(0xFF, 0xFF, 0xFF)

_LANG_LABELS: dict[str, str] = {
    "fr": "Français", "en": "English", "de": "Deutsch",
    "it": "Italiano", "es": "Español",
}
_MONTHS_FR = {
    1: "janvier", 2: "février", 3: "mars", 4: "avril",
    5: "mai", 6: "juin", 7: "juillet", 8: "août",
    9: "septembre", 10: "octobre", 11: "novembre", 12: "décembre",
}

# ── Helpers XML ──────────────────────────────────────────────────────────────

def _hex(c: RGBColor) -> str:
    return f"{c[0]:02X}{c[1]:02X}{c[2]:02X}"


def _cell_bg(cell: Any, color: RGBColor) -> None:
    tcPr = cell._tc.get_or_add_tcPr()
    for old in tcPr.findall(qn("w:shd")):
        tcPr.remove(old)
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), _hex(color))
    tcPr.append(shd)


def _cell_margins(cell: Any, top: int = 60, bottom: int = 60, left: int = 120, right: int = 120) -> None:
    tcPr = cell._tc.get_or_add_tcPr()
    tcMar = OxmlElement("w:tcMar")
    for side, val in (("top", top), ("left", left), ("bottom", bottom), ("right", right)):
        el = OxmlElement(f"w:{side}")
        el.set(qn("w:w"), str(val))
        el.set(qn("w:type"), "dxa")
        tcMar.append(el)
    tcPr.append(tcMar)


def _table_full_width(table: Any) -> None:
    tbl = table._tbl
    tblPr = tbl.find(qn("w:tblPr"))
    if tblPr is None:
        tblPr = OxmlElement("w:tblPr")
        tbl.insert(0, tblPr)
    tblW = OxmlElement("w:tblW")
    tblW.set(qn("w:w"), "5000")
    tblW.set(qn("w:type"), "pct")
    tblPr.append(tblW)


def _table_no_borders(table: Any) -> None:
    tbl = table._tbl
    tblPr = tbl.find(qn("w:tblPr"))
    if tblPr is None:
        tblPr = OxmlElement("w:tblPr")
        tbl.insert(0, tblPr)
    borders = OxmlElement("w:tblBorders")
    for name in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{name}")
        el.set(qn("w:val"), "none")
        borders.append(el)
    tblPr.append(borders)


def _table_thin_borders(table: Any) -> None:
    tbl = table._tbl
    tblPr = tbl.find(qn("w:tblPr"))
    if tblPr is None:
        tblPr = OxmlElement("w:tblPr")
        tbl.insert(0, tblPr)
    borders = OxmlElement("w:tblBorders")
    for name in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{name}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), "4")
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), "BFBFBF")
        borders.append(el)
    tblPr.append(borders)


def _para_bottom_border(para: Any, color: RGBColor, sz: int = 6) -> None:
    pPr = para._p.get_or_add_pPr()
    pBdr = OxmlElement("w:pBdr")
    b = OxmlElement("w:bottom")
    b.set(qn("w:val"), "single")
    b.set(qn("w:sz"), str(sz))
    b.set(qn("w:space"), "1")
    b.set(qn("w:color"), _hex(color))
    pBdr.append(b)
    pPr.append(pBdr)


def _add_page_number_field(run: Any) -> None:
    fldChar1 = OxmlElement("w:fldChar")
    fldChar1.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.text = "PAGE"
    fldChar2 = OxmlElement("w:fldChar")
    fldChar2.set(qn("w:fldCharType"), "end")
    run._r.append(fldChar1)
    run._r.append(instr)
    run._r.append(fldChar2)


def _add_num_pages_field(run: Any) -> None:
    fldChar1 = OxmlElement("w:fldChar")
    fldChar1.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.text = "NUMPAGES"
    fldChar2 = OxmlElement("w:fldChar")
    fldChar2.set(qn("w:fldCharType"), "end")
    run._r.append(fldChar1)
    run._r.append(instr)
    run._r.append(fldChar2)


# ── Formatage ────────────────────────────────────────────────────────────────

def _fmt_date(date_str: str) -> str:
    if not date_str:
        return "—"
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return f"{dt.day} {_MONTHS_FR[dt.month]} {dt.year}"
    except ValueError:
        return date_str


def _fmt_time(seconds: float) -> str:
    s = int(seconds)
    m, sec = divmod(s, 60)
    return f"{m}min {sec:02d}s" if m else f"{sec}s"


# ── Parsing SRT ───────────────────────────────────────────────────────────────

_SRT_BLOCK = re.compile(
    r"\d+\s*\n"
    r"(\d{2}:\d{2}:\d{2}),\d{3}\s*-->\s*\d{2}:\d{2}:\d{2},\d{3}\s*\n"
    r"(.*?)(?=\n\n|\Z)",
    re.DOTALL,
)
_SPEAKER_LINE = re.compile(r"^[A-Z_0-9]+\(([^)]+)\):\s*(.+)$")


def _parse_srt(srt_text: str) -> list[dict[str, str]]:
    """Retourne une liste de {"timestamp": "HH:MM:SS", "speaker": str, "text": str}."""
    entries: list[dict[str, str]] = []
    for m in _SRT_BLOCK.finditer(srt_text.strip()):
        timestamp = m.group(1)
        body = m.group(2).strip()
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            sm = _SPEAKER_LINE.match(line)
            if sm:
                entries.append({
                    "timestamp": timestamp,
                    "speaker": sm.group(1),
                    "text": sm.group(2),
                })
            else:
                entries.append({"timestamp": timestamp, "speaker": "", "text": line})
    return entries


# ── Classe principale ─────────────────────────────────────────────────────────

class DocxReport:
    def __init__(
        self,
        ctx: dict,
        participants: list,
        speaker_stats: dict,
        quality: dict,
        srt_text: str,
    ):
        self.ctx = ctx
        self.participants: list[dict] = participants if isinstance(participants, list) else []
        self.speakers: list[dict] = (speaker_stats or {}).get("speakers", [])
        self.quality = quality or {}
        self.srt_entries = _parse_srt(srt_text)
        self.merged = self._merge_participants()

    # ── Fusion participants ───────────────────────────────────────────────────

    def _merge_participants(self) -> list[dict]:
        spk_map = {s["mapped_to"]: s for s in self.speakers if s.get("mapped_to")}
        total_time = sum(s.get("speaking_time_seconds", 0.0) for s in self.speakers)
        result: list[dict] = []

        for p in self.participants:
            spk = spk_map.get(p["id"], {})
            time_s = float(spk.get("speaking_time_seconds", 0))
            pct = round(100 * time_s / max(total_time, 0.001))
            result.append({
                "name": (p.get("name") or spk.get("mapped_name") or "—").strip(),
                "function": p.get("function", ""),
                "service": p.get("service", ""),
                "role": p.get("role", ""),
                "is_animator": bool(p.get("is_animator", False)),
                "time_s": time_s,
                "time_pct": pct,
                "turns": spk.get("turn_count", "—"),
            })

        mapped_ids = {p.get("id") for p in self.participants}
        for spk in self.speakers:
            if spk.get("mapped_to") not in mapped_ids:
                time_s = float(spk.get("speaking_time_seconds", 0))
                pct = round(100 * time_s / max(total_time, 0.001))
                result.append({
                    "name": (spk.get("mapped_name") or spk.get("speaker_id") or "—"),
                    "function": "",
                    "service": "",
                    "role": "",
                    "is_animator": False,
                    "time_s": time_s,
                    "time_pct": pct,
                    "turns": spk.get("turn_count", "—"),
                })
        return result

    # ── Build ─────────────────────────────────────────────────────────────────

    def build(self) -> Document:
        doc = Document()
        self._setup_document(doc)
        self._cover_page(doc)
        self._page_break(doc)
        self._section_context(doc)
        self._section_participants(doc)
        self._section_transcript(doc)
        self._section_quality(doc)
        self._setup_footer(doc)
        return doc

    # ── Setup ─────────────────────────────────────────────────────────────────

    def _setup_document(self, doc: Document) -> None:
        for section in doc.sections:
            section.top_margin    = Cm(2.0)
            section.bottom_margin = Cm(2.0)
            section.left_margin   = Cm(2.5)
            section.right_margin  = Cm(2.5)

    # ── Page de garde ─────────────────────────────────────────────────────────

    def _cover_page(self, doc: Document) -> None:
        ctx = self.ctx
        title  = ctx.get("title") or "Sans titre"
        mtype  = ctx.get("meeting_type", "")
        date   = _fmt_date(ctx.get("date", ""))
        svc    = ctx.get("service", "") or "—"
        lang   = _LANG_LABELS.get(ctx.get("language", "fr"), ctx.get("language", ""))
        sensitivity = ctx.get("sensitivity", "normal")
        score  = self.quality.get("quality_score")

        # ── Bandeau bleu pleine largeur ──────────────────────────────────────
        hdr_table = doc.add_table(rows=1, cols=1)
        _table_full_width(hdr_table)
        _table_no_borders(hdr_table)
        cell = hdr_table.cell(0, 0)
        _cell_bg(cell, _BLUE_DARK)
        _cell_margins(cell, top=200, bottom=200, left=300, right=300)
        p = cell.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run("COMPTE-RENDU DE TRANSCRIPTION")
        run.font.color.rgb = _WHITE
        run.font.bold = True
        run.font.size = Pt(13)
        run.font.name = "Calibri"

        # ── Badge CONFIDENTIEL ───────────────────────────────────────────────
        if sensitivity == "high":
            conf_table = doc.add_table(rows=1, cols=1)
            _table_full_width(conf_table)
            _table_no_borders(conf_table)
            conf_cell = conf_table.cell(0, 0)
            _cell_bg(conf_cell, _RED)
            _cell_margins(conf_cell, top=100, bottom=100, left=200, right=200)
            p2 = conf_cell.paragraphs[0]
            p2.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run2 = p2.add_run("⚠  DOCUMENT CONFIDENTIEL  ⚠")
            run2.font.color.rgb = _WHITE
            run2.font.bold = True
            run2.font.size = Pt(10)
            run2.font.name = "Calibri"
        else:
            doc.add_paragraph()

        # ── Titre ────────────────────────────────────────────────────────────
        doc.add_paragraph()
        p_title = doc.add_paragraph()
        p_title.alignment = WD_ALIGN_PARAGRAPH.LEFT
        run_title = p_title.add_run(title.upper())
        run_title.font.size = Pt(20)
        run_title.font.bold = True
        run_title.font.color.rgb = _BLUE_DARK
        run_title.font.name = "Calibri"
        _para_bottom_border(p_title, _BLUE_MID, sz=8)

        doc.add_paragraph()

        # ── Métadonnées (table 2 colonnes) ───────────────────────────────────
        meta_rows = [
            ("Type de réunion", mtype or "—"),
            ("Date",            date),
            ("Service",         svc),
            ("Langue",          lang or "—"),
        ]
        meta_table = doc.add_table(rows=len(meta_rows), cols=2)
        _table_full_width(meta_table)
        _table_no_borders(meta_table)

        for i, (label, value) in enumerate(meta_rows):
            cells = meta_table.rows[i].cells
            _cell_margins(cells[0], top=40, bottom=40, left=0, right=80)
            _cell_margins(cells[1], top=40, bottom=40, left=80, right=0)

            r_lbl = cells[0].paragraphs[0].add_run(label)
            r_lbl.font.size = Pt(10)
            r_lbl.font.bold = True
            r_lbl.font.color.rgb = _GREY_DARK
            r_lbl.font.name = "Calibri"

            r_val = cells[1].paragraphs[0].add_run(value)
            r_val.font.size = Pt(10)
            r_val.font.color.rgb = _BLUE_DARK
            r_val.font.name = "Calibri"

        doc.add_paragraph()
        doc.add_paragraph()

        # ── Pied de page de garde ────────────────────────────────────────────
        p_gen = doc.add_paragraph()
        p_gen.alignment = WD_ALIGN_PARAGRAPH.CENTER

        r1 = p_gen.add_run(f"Généré par TranscrIA  ▪  {datetime.today().strftime('%d/%m/%Y')}")
        r1.font.size = Pt(9)
        r1.font.color.rgb = _GREY_DARK
        r1.font.name = "Calibri"

        if score is not None:
            score_color = _GREEN if score >= 85 else _ORANGE if score >= 65 else _RED
            r2 = p_gen.add_run(f"  ▪  Score qualité : {score}/100")
            r2.font.size = Pt(9)
            r2.font.color.rgb = score_color
            r2.font.bold = True
            r2.font.name = "Calibri"

    # ── Helpers section ───────────────────────────────────────────────────────

    @staticmethod
    def _section_heading(doc: Document, number: str, label: str) -> None:
        doc.add_paragraph()
        p = doc.add_paragraph()
        r_num = p.add_run(f"{number}  ")
        r_num.font.size = Pt(13)
        r_num.font.bold = True
        r_num.font.color.rgb = _BLUE_MID
        r_num.font.name = "Calibri"
        r_lbl = p.add_run(label.upper())
        r_lbl.font.size = Pt(13)
        r_lbl.font.bold = True
        r_lbl.font.color.rgb = _BLUE_DARK
        r_lbl.font.name = "Calibri"
        _para_bottom_border(p, _BLUE_MID, sz=6)

    @staticmethod
    def _meta_row(doc: Document, label: str, value: str) -> None:
        p = doc.add_paragraph()
        r_lbl = p.add_run(f"{label} : ")
        r_lbl.font.size = Pt(10)
        r_lbl.font.bold = True
        r_lbl.font.color.rgb = _GREY_DARK
        r_lbl.font.name = "Calibri"
        r_val = p.add_run(value)
        r_val.font.size = Pt(10)
        r_val.font.name = "Calibri"
        p.paragraph_format.space_after = Pt(2)

    @staticmethod
    def _page_break(doc: Document) -> None:
        doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    # ── Section 1 : Contexte ──────────────────────────────────────────────────

    def _section_context(self, doc: Document) -> None:
        ctx = self.ctx
        self._section_heading(doc, "1.", "Contexte de la réunion")

        topic = ctx.get("topic", "").strip()
        objective = ctx.get("objective", "").strip()
        notes = ctx.get("notes", "").strip()
        summary = (ctx.get("summary") or ctx.get("summary_llm") or "").strip()

        if topic:
            self._meta_row(doc, "Sujet", topic)
        if objective:
            self._meta_row(doc, "Objectif", objective)
        if notes and notes.lower() not in ("n/a", "n/a — scène unique de dialogue.", ""):
            self._meta_row(doc, "Notes / Ordre du jour", notes)

        if summary:
            doc.add_paragraph()
            # Extraire juste le paragraphe "Synthèse" si présent dans un markdown
            synth = _extract_synthese(summary)
            p_head = doc.add_paragraph()
            r = p_head.add_run("Synthèse")
            r.font.size = Pt(11)
            r.font.bold = True
            r.font.color.rgb = _BLUE_DARK
            r.font.name = "Calibri"
            p_head.paragraph_format.space_after = Pt(4)

            for line in synth.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Nettoyer le markdown léger (**, __)
                line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
                line = re.sub(r"__(.+?)__", r"\1", line)
                p = doc.add_paragraph(line)
                p.paragraph_format.space_after = Pt(3)
                for run in p.runs:
                    run.font.size = Pt(10)
                    run.font.name = "Calibri"

    # ── Section 2 : Participants ──────────────────────────────────────────────

    def _section_participants(self, doc: Document) -> None:
        self._section_heading(doc, "2.", "Participants & Locuteurs")

        if not self.merged:
            doc.add_paragraph("Aucun participant enregistré.")
            return

        has_function = any(p["function"] for p in self.merged)
        has_service  = any(p["service"]  for p in self.merged)
        has_animator = any(p["is_animator"] for p in self.merged)

        headers = ["Nom"]
        if has_function:
            headers.append("Fonction")
        if has_service:
            headers.append("Service")
        headers += ["Rôle", "Tps de parole", "Interventions"]
        if has_animator:
            headers.append("")

        n_cols = len(headers)
        table = doc.add_table(rows=1 + len(self.merged), cols=n_cols)
        _table_full_width(table)
        _table_thin_borders(table)

        # ── En-tête ──────────────────────────────────────────────────────────
        hdr_cells = table.rows[0].cells
        for i, h in enumerate(headers):
            _cell_bg(hdr_cells[i], _BLUE_DARK)
            _cell_margins(hdr_cells[i], top=80, bottom=80, left=100, right=100)
            p = hdr_cells[i].paragraphs[0]
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = p.add_run(h)
            run.font.bold = True
            run.font.color.rgb = _WHITE
            run.font.size = Pt(9.5)
            run.font.name = "Calibri"

        # ── Lignes données ────────────────────────────────────────────────────
        for row_i, participant in enumerate(self.merged):
            row = table.rows[row_i + 1]
            bg = _BLUE_LIGHT if row_i % 2 == 0 else _WHITE

            col = 0
            data: list[tuple[str, bool, RGBColor]] = []  # (text, bold, color)

            data.append((participant["name"], True, _BLUE_DARK))
            if has_function:
                data.append((participant["function"] or "—", False, _GREY_DARK))
            if has_service:
                data.append((participant["service"] or "—", False, _GREY_DARK))
            role = participant["role"][:80] + "…" if len(participant["role"]) > 80 else participant["role"]
            data.append((role or "—", False, _GREY_DARK))
            time_label = (
                f"{_fmt_time(participant['time_s'])} ({participant['time_pct']}%)"
                if participant["time_s"] > 0 else "—"
            )
            data.append((time_label, False, _GREY_DARK))
            data.append((str(participant["turns"]), False, _GREY_DARK))
            if has_animator:
                data.append(("★ Animateur" if participant["is_animator"] else "", True, _BLUE_MID))

            for text, bold, color in data:
                cell = row.cells[col]
                _cell_bg(cell, bg)
                _cell_margins(cell, top=60, bottom=60, left=100, right=100)
                p = cell.paragraphs[0]
                run = p.add_run(text)
                run.font.size = Pt(9.5)
                run.font.bold = bold
                run.font.color.rgb = color
                run.font.name = "Calibri"
                col += 1

    # ── Section 3 : Transcription ─────────────────────────────────────────────

    def _section_transcript(self, doc: Document) -> None:
        self._section_heading(doc, "3.", "Transcription")

        if not self.srt_entries:
            doc.add_paragraph("Aucune transcription disponible.")
            return

        table = doc.add_table(rows=len(self.srt_entries), cols=3)
        _table_full_width(table)
        _table_no_borders(table)

        for i, entry in enumerate(self.srt_entries):
            row = table.rows[i]
            bg = _GREY_LIGHT if i % 2 == 0 else _WHITE

            # Col 0 — timestamp
            ts_cell = row.cells[0]
            _cell_bg(ts_cell, bg)
            _cell_margins(ts_cell, top=40, bottom=40, left=0, right=60)
            p0 = ts_cell.paragraphs[0]
            r0 = p0.add_run(entry["timestamp"])
            r0.font.size = Pt(8)
            r0.font.italic = True
            r0.font.color.rgb = _GREY_DARK
            r0.font.name = "Consolas"

            # Col 1 — locuteur
            spk_cell = row.cells[1]
            _cell_bg(spk_cell, bg)
            _cell_margins(spk_cell, top=40, bottom=40, left=60, right=80)
            p1 = spk_cell.paragraphs[0]
            r1 = p1.add_run(entry["speaker"])
            r1.font.size = Pt(9)
            r1.font.bold = True
            r1.font.color.rgb = _BLUE_MID if entry["speaker"] else _GREY_DARK
            r1.font.name = "Calibri"

            # Col 2 — texte
            txt_cell = row.cells[2]
            _cell_bg(txt_cell, bg)
            _cell_margins(txt_cell, top=40, bottom=40, left=80, right=0)
            p2 = txt_cell.paragraphs[0]
            r2 = p2.add_run(entry["text"])
            r2.font.size = Pt(9.5)
            r2.font.name = "Calibri"

    # ── Section 4 : Points à vérifier (conditionnelle) ────────────────────────

    def _section_quality(self, doc: Document) -> None:
        checks = self.quality.get("checks", [])
        points: list[tuple[str, str]] = []  # (emoji_label, description)

        for check in checks:
            ctype = check.get("type")
            sev   = check.get("severity", "info")
            if sev == "info":
                continue

            if ctype == "low_coverage":
                ratio = check.get("ratio", 1.0)
                if ratio < 0.85:
                    pct = round(ratio * 100)
                    points.append(("⚠  Couverture audio", f"{pct}% — possible perte de transcription"))

            elif ctype == "audio_problem_segments":
                examples = check.get("examples", [])
                for ex in examples:
                    label = ex.get("label", "anomalie")
                    s = ex.get("start_label", "")
                    e = ex.get("end_label", "")
                    points.append(("🔍  Zone à réécouter", f"{s} → {e} ({label})"))

            elif ctype == "unresolved_lexicon_variants":
                for ev in check.get("exact_variants", []):
                    points.append(("✎  Terme à valider", f"{ev['term']} (variante : {ev['variant']})"))
                for cf in check.get("close_forms", []):
                    points.append(("✎  Orthographe à vérifier", f"{cf['form']} proche de {cf['term']}"))

        if not points:
            return

        self._section_heading(doc, "4.", "Points à vérifier")

        table = doc.add_table(rows=len(points), cols=2)
        _table_full_width(table)
        _table_no_borders(table)

        for i, (label, desc) in enumerate(points):
            cells = table.rows[i].cells
            _cell_bg(cells[0], _YELLOW_BG)
            _cell_bg(cells[1], _YELLOW_BG)
            _cell_margins(cells[0], top=60, bottom=60, left=100, right=80)
            _cell_margins(cells[1], top=60, bottom=60, left=80, right=100)

            r0 = cells[0].paragraphs[0].add_run(label)
            r0.font.size = Pt(9.5)
            r0.font.bold = True
            r0.font.color.rgb = RGBColor(0x7B, 0x36, 0x06)
            r0.font.name = "Calibri"

            r1 = cells[1].paragraphs[0].add_run(desc)
            r1.font.size = Pt(9.5)
            r1.font.color.rgb = RGBColor(0x7B, 0x36, 0x06)
            r1.font.name = "Calibri"

    # ── Pied de page ──────────────────────────────────────────────────────────

    def _setup_footer(self, doc: Document) -> None:
        title  = (self.ctx.get("title") or "TranscrIA")[:40]
        date   = _fmt_date(self.ctx.get("date", ""))
        score  = self.quality.get("quality_score")

        section = doc.sections[0]
        footer  = section.footer
        footer.is_linked_to_previous = False
        p = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
        p.clear()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER

        if score is not None:
            score_color = _GREEN if score >= 85 else _ORANGE if score >= 65 else _RED
            r_score = p.add_run(f"■ {score}/100   ")
            r_score.font.size = Pt(7.5)
            r_score.font.color.rgb = score_color
            r_score.font.name = "Calibri"

        r_info = p.add_run(f"TranscrIA — {title}  |  {date}  |  Page ")
        r_info.font.size = Pt(7.5)
        r_info.font.color.rgb = _GREY_DARK
        r_info.font.name = "Calibri"

        r_pg = p.add_run()
        r_pg.font.size = Pt(7.5)
        r_pg.font.color.rgb = _GREY_DARK
        _add_page_number_field(r_pg)

        r_sep = p.add_run(" / ")
        r_sep.font.size = Pt(7.5)
        r_sep.font.color.rgb = _GREY_DARK
        r_sep.font.name = "Calibri"

        r_tot = p.add_run()
        r_tot.font.size = Pt(7.5)
        r_tot.font.color.rgb = _GREY_DARK
        _add_num_pages_field(r_tot)


# ── Extraction synthèse depuis markdown ──────────────────────────────────────

def _extract_synthese(text: str) -> str:
    """Extrait la section 'Synthèse' d'un markdown LLM si présente, sinon retourne le texte brut."""
    m = re.search(r"##\s*Synth[eè]se\s*\n(.*?)(?=\n##|\Z)", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # Fallback : retourner le texte sans les titres markdown
    cleaned = re.sub(r"^#{1,3}\s+.+$", "", text, flags=re.MULTILINE)
    return cleaned.strip()


# ── Point d'entrée public ─────────────────────────────────────────────────────

def generate_docx_report(job_id: str, jobs_dir: str, output_path: Path) -> Path:
    """
    Génère le rapport DOCX pour un job terminé et l'écrit dans output_path.

    Retourne le chemin du fichier généré.
    """
    fs = JobFilesystem(jobs_dir, job_id)

    ctx           = fs.load_json("context/meeting_context.json") or {}
    participants  = fs.load_json("context/participants.json") or []
    speaker_stats = fs.load_json("speakers/speaker_stats.json") or {}
    quality       = fs.load_json("quality/quality_report.json") or {}

    srt_text = fs.load_text("metadata/transcription_corrigee.srt") or ""
    if not srt_text:
        srt_text = fs.load_text("metadata/transcription.srt") or ""

    report = DocxReport(ctx, participants, speaker_stats, quality, srt_text)
    doc = report.build()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    return output_path

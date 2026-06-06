"""Génère un rapport DOCX professionnel à partir des artefacts d'un job terminé."""
from __future__ import annotations

import re
from dataclasses import dataclass
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


# ── Système de thèmes visuels ─────────────────────────────────────────────────

@dataclass
class _DocxTheme:
    primary:     RGBColor   # bannière, titres de section, en-têtes tableaux
    accent:      RGBColor   # bordures, bullets, éléments secondaires
    light:       RGBColor   # lignes alternées tableaux
    banner_text: str        # texte du bandeau de couverture
    cover_badge: str        # badge type affiché sous le titre (ex: "RÉUNION PROJET")


def _rgb(r: int, g: int, b: int) -> RGBColor:
    return RGBColor(r, g, b)


_THEMES: dict[str, _DocxTheme] = {
    # ── Institutionnel / légal ──────────────────────────────────────────────
    "CSE": _DocxTheme(
        primary=_rgb(0x1A, 0x23, 0x7E), accent=_rgb(0x30, 0x3F, 0x9F),
        light=_rgb(0xE8, 0xEA, 0xF6),
        banner_text="PROCÈS-VERBAL DU COMITÉ SOCIAL ET ÉCONOMIQUE",
        cover_badge="CSE",
    ),
    "CSE extraordinaire": _DocxTheme(
        primary=_rgb(0x1A, 0x23, 0x7E), accent=_rgb(0x30, 0x3F, 0x9F),
        light=_rgb(0xE8, 0xEA, 0xF6),
        banner_text="PROCÈS-VERBAL DU CSE — SÉANCE EXTRAORDINAIRE",
        cover_badge="CSE EXTRA",
    ),
    # ── Direction / stratégie ───────────────────────────────────────────────
    "CODIR / COMEX": _DocxTheme(
        primary=_rgb(0x1C, 0x1C, 0x1C), accent=_rgb(0x42, 0x42, 0x42),
        light=_rgb(0xF5, 0xF5, 0xF5),
        banner_text="COMPTE-RENDU — COMITÉ DE DIRECTION",
        cover_badge="CODIR",
    ),
    # ── Projet ─────────────────────────────────────────────────────────────
    "Point projet": _DocxTheme(
        primary=_rgb(0x00, 0x4D, 0x40), accent=_rgb(0x00, 0x79, 0x5F),
        light=_rgb(0xE0, 0xF2, 0xF1),
        banner_text="COMPTE-RENDU DE RÉUNION PROJET",
        cover_badge="PROJET",
    ),
    "Réunion projet": _DocxTheme(
        primary=_rgb(0x00, 0x4D, 0x40), accent=_rgb(0x00, 0x79, 0x5F),
        light=_rgb(0xE0, 0xF2, 0xF1),
        banner_text="COMPTE-RENDU DE RÉUNION PROJET",
        cover_badge="PROJET",
    ),
    # ── Client / commercial ─────────────────────────────────────────────────
    "Réunion client": _DocxTheme(
        primary=_rgb(0x01, 0x4B, 0x7E), accent=_rgb(0x02, 0x77, 0xBD),
        light=_rgb(0xE1, 0xF5, 0xFE),
        banner_text="COMPTE-RENDU DE RÉUNION CLIENT",
        cover_badge="CLIENT",
    ),
    "Négociation": _DocxTheme(
        primary=_rgb(0x3E, 0x27, 0x23), accent=_rgb(0x6D, 0x4C, 0x41),
        light=_rgb(0xEF, 0xEB, 0xE9),
        banner_text="COMPTE-RENDU DE NÉGOCIATION",
        cover_badge="NÉGOCIATION",
    ),
    # ── RH / personnes ─────────────────────────────────────────────────────
    "RH": _DocxTheme(
        primary=_rgb(0x1B, 0x5E, 0x20), accent=_rgb(0x43, 0x8B, 0x29),
        light=_rgb(0xF1, 0xF8, 0xE9),
        banner_text="COMPTE-RENDU — RESSOURCES HUMAINES",
        cover_badge="RH",
    ),
    "Entretien individuel": _DocxTheme(
        primary=_rgb(0x4A, 0x14, 0x8C), accent=_rgb(0x6A, 0x1B, 0x9A),
        light=_rgb(0xF3, 0xE5, 0xF5),
        banner_text="ENTRETIEN INDIVIDUEL",
        cover_badge="CONFIDENTIEL",
    ),
    "Entretien": _DocxTheme(
        primary=_rgb(0x1F, 0x38, 0x64), accent=_rgb(0x2E, 0x74, 0xB5),
        light=_rgb(0xD6, 0xE4, 0xF0),
        banner_text="COMPTE-RENDU D'ENTRETIEN",
        cover_badge="ENTRETIEN",
    ),
    # ── Formation / pédagogie ───────────────────────────────────────────────
    "Formation": _DocxTheme(
        primary=_rgb(0xBF, 0x36, 0x00), accent=_rgb(0xF5, 0x7C, 0x00),
        light=_rgb(0xFF, 0xF3, 0xE0),
        banner_text="COMPTE-RENDU DE FORMATION",
        cover_badge="FORMATION",
    ),
    "Séminaire / atelier": _DocxTheme(
        primary=_rgb(0x00, 0x57, 0x47), accent=_rgb(0x00, 0x79, 0x5F),
        light=_rgb(0xE0, 0xF7, 0xF2),
        banner_text="COMPTE-RENDU DE SÉMINAIRE / ATELIER",
        cover_badge="SÉMINAIRE",
    ),
    # ── Urgence / médical ───────────────────────────────────────────────────
    "Réunion de crise": _DocxTheme(
        primary=_rgb(0xB7, 0x1C, 0x1C), accent=_rgb(0xE5, 0x39, 0x35),
        light=_rgb(0xFF, 0xEB, 0xEE),
        banner_text="COMPTE-RENDU — RÉUNION DE CRISE",
        cover_badge="CRISE",
    ),
    "Réunion médicale / santé": _DocxTheme(
        primary=_rgb(0x00, 0x60, 0x64), accent=_rgb(0x00, 0x83, 0x8A),
        light=_rgb(0xE0, 0xF7, 0xFA),
        banner_text="COMPTE-RENDU MÉDICAL",
        cover_badge="CONFIDENTIEL",
    ),
    # ── Technique ───────────────────────────────────────────────────────────
    "Réunion technique": _DocxTheme(
        primary=_rgb(0x1F, 0x38, 0x64), accent=_rgb(0x29, 0x63, 0x9A),
        light=_rgb(0xE3, 0xEE, 0xF8),
        banner_text="COMPTE-RENDU DE RÉUNION TECHNIQUE",
        cover_badge="TECHNIQUE",
    ),
    "Podcast / média": _DocxTheme(
        primary=_rgb(0x22, 0x00, 0x57), accent=_rgb(0x5E, 0x35, 0xB1),
        light=_rgb(0xED, 0xE7, 0xF6),
        banner_text="TRANSCRIPTION",
        cover_badge="MÉDIA",
    ),
}

_THEME_DEFAULT = _DocxTheme(
    primary=_BLUE_DARK, accent=_BLUE_MID, light=_BLUE_LIGHT,
    banner_text="COMPTE-RENDU DE TRANSCRIPTION",
    cover_badge="",
)


def _get_theme(meeting_type: str) -> _DocxTheme:
    return _THEMES.get(meeting_type, _THEME_DEFAULT)


# ── Routing par type de réunion ──────────────────────────────────────────────

# Les sections enrichies (décisions, votes, ODJ…) ne sont PAS filtrées par type :
# toute donnée extraite par le LLM s'affiche si elle est non vide (cf. _section_enriched).
# Le type ne pilote que le thème visuel, les champs de saisie et la page de garde.

# Types CSE — quorum + sous-titre objet de séance sur la page de garde
_CSE_TYPES: frozenset[str] = frozenset({"CSE", "CSE extraordinaire"})
# Types auto-confidentiels
_AUTO_CONFIDENTIEL: frozenset[str] = frozenset({"Entretien individuel", "RH", "Réunion médicale / santé"})

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
        structured_data: dict | None = None,
    ):
        self.ctx = ctx
        self.participants: list[dict] = participants if isinstance(participants, list) else []
        self.speakers: list[dict] = (speaker_stats or {}).get("speakers", [])
        self.quality = quality or {}
        self.srt_entries = _parse_srt(srt_text)
        self.merged = self._merge_participants()
        self.structured_data: dict = structured_data or {}
        self.meeting_type: str = ctx.get("meeting_type", "") if ctx else ""
        self.type_specific_data: dict = ctx.get("type_specific_data") or {}
        self.theme: _DocxTheme = _get_theme(self.meeting_type)
        # Auto-confidentialité pour certains types
        if self.meeting_type in _AUTO_CONFIDENTIEL and not ctx.get("sensitivity"):
            self.ctx = dict(ctx)
            self.ctx["sensitivity"] = "high"

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
        offset = self._section_enriched(doc)
        self._section_participants(doc, base=2 + offset)
        self._section_transcript(doc, base=3 + offset)
        self._section_quality(doc, base=4 + offset)
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

    def _cover_page(self, doc: Document) -> None:  # noqa: C901
        ctx    = self.ctx
        theme  = self.theme
        title  = ctx.get("title") or "Sans titre"
        mtype  = ctx.get("meeting_type", "")
        date   = _fmt_date(ctx.get("date", ""))
        svc    = ctx.get("service", "") or ""
        lang   = _LANG_LABELS.get(ctx.get("language", "fr"), ctx.get("language", ""))
        sensitivity = ctx.get("sensitivity", "normal")
        score  = self.quality.get("quality_score")
        ts     = self.type_specific_data

        # ── 1. Bandeau principal (couleur signature du type) ─────────────────
        hdr = doc.add_table(rows=1, cols=1)
        _table_full_width(hdr)
        _table_no_borders(hdr)
        hdr_cell = hdr.cell(0, 0)
        _cell_bg(hdr_cell, theme.primary)
        _cell_margins(hdr_cell, top=260, bottom=260, left=360, right=360)
        p_hdr = hdr_cell.paragraphs[0]
        p_hdr.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r_hdr = p_hdr.add_run(theme.banner_text)
        r_hdr.font.color.rgb = _WHITE
        r_hdr.font.bold = True
        r_hdr.font.size = Pt(12)
        r_hdr.font.name = "Calibri"

        # ── 2. Trait d'accent mince sous le bandeau ──────────────────────────
        acc = doc.add_table(rows=1, cols=1)
        _table_full_width(acc)
        _table_no_borders(acc)
        acc_cell = acc.cell(0, 0)
        _cell_bg(acc_cell, theme.accent)
        _cell_margins(acc_cell, top=24, bottom=24, left=0, right=0)
        acc_cell.paragraphs[0].add_run("")

        # ── 3. Badge CONFIDENTIEL / CRISE (si applicable) ────────────────────
        is_confidentiel = (sensitivity == "high") or (mtype in _AUTO_CONFIDENTIEL)
        is_crise = mtype == "Réunion de crise"
        if is_confidentiel or is_crise:
            badge_color = _RED if is_crise else RGBColor(0x6A, 0x1B, 0x9A)
            badge_text  = ("⚠  SITUATION DE CRISE  ⚠" if is_crise
                           else "▪  DOCUMENT CONFIDENTIEL  ▪")
            conf_t = doc.add_table(rows=1, cols=1)
            _table_full_width(conf_t)
            _table_no_borders(conf_t)
            conf_cell = conf_t.cell(0, 0)
            _cell_bg(conf_cell, badge_color)
            _cell_margins(conf_cell, top=80, bottom=80, left=200, right=200)
            p_conf = conf_cell.paragraphs[0]
            p_conf.alignment = WD_ALIGN_PARAGRAPH.CENTER
            r_conf = p_conf.add_run(badge_text)
            r_conf.font.color.rgb = _WHITE
            r_conf.font.bold = True
            r_conf.font.size = Pt(9)
            r_conf.font.name = "Calibri"

        # ── 4. Titre principal ───────────────────────────────────────────────
        doc.add_paragraph()
        doc.add_paragraph()
        p_title = doc.add_paragraph()
        p_title.alignment = WD_ALIGN_PARAGRAPH.LEFT
        run_title = p_title.add_run(title.upper())
        run_title.font.size = Pt(22)
        run_title.font.bold = True
        run_title.font.color.rgb = theme.primary
        run_title.font.name = "Calibri"
        _para_bottom_border(p_title, theme.accent, sz=10)

        # ── 5. Sous-titre contextuel (nom projet, objet CSE extra…) ──────────
        subtitle_parts: list[str] = []
        if mtype in ("Point projet", "Réunion projet") and ts.get("nom_projet"):
            subtitle_parts.append(ts["nom_projet"])
            if ts.get("phase_jalon"):
                subtitle_parts.append(ts["phase_jalon"])
        elif mtype in _CSE_TYPES and ts.get("objet_seance"):
            subtitle_parts.append(ts["objet_seance"])
        elif mtype == "Réunion client" and ts.get("nom_client"):
            subtitle_parts.append(ts["nom_client"])
        elif mtype == "Réunion de crise" and ts.get("nature_incident"):
            subtitle_parts.append(ts["nature_incident"])
        elif mtype == "Entretien individuel" and ts.get("poste_evalue"):
            subtitle_parts.append(f"Entretien — {ts['poste_evalue']}")

        if subtitle_parts:
            p_sub = doc.add_paragraph()
            r_sub = p_sub.add_run("  ".join(subtitle_parts))
            r_sub.font.size = Pt(12)
            r_sub.font.color.rgb = theme.accent
            r_sub.font.italic = True
            r_sub.font.name = "Calibri"
            p_sub.paragraph_format.space_before = Pt(4)
            p_sub.paragraph_format.space_after = Pt(0)

        doc.add_paragraph()

        # ── 6. Métadonnées en 2 colonnes soignées ────────────────────────────
        meta_rows: list[tuple[str, str]] = []
        if date and date != "—":
            meta_rows.append(("Date", date))
        if mtype:
            meta_rows.append(("Type", mtype))
        if svc:
            meta_rows.append(("Service", svc))
        if lang:
            meta_rows.append(("Langue", lang))
        # Champs type-spécifiques clés sur la couverture
        if mtype in _CSE_TYPES:
            if ts.get("president_seance"):
                meta_rows.append(("Président de séance", ts["president_seance"]))
            if ts.get("secretaire_seance"):
                meta_rows.append(("Secrétaire de séance", ts["secretaire_seance"]))
            if ts.get("ref_pv_precedent"):
                meta_rows.append(("Réf. PV précédent", ts["ref_pv_precedent"]))
        elif mtype in ("Point projet", "Réunion projet"):
            if ts.get("chef_de_projet"):
                meta_rows.append(("Chef de projet", ts["chef_de_projet"]))
            if ts.get("sprint"):
                meta_rows.append(("Sprint", ts["sprint"]))
        elif mtype == "CODIR / COMEX":
            pass  # ordre du jour dans le document
        elif mtype == "Réunion client":
            if ts.get("ref_contrat"):
                meta_rows.append(("Réf. contrat", ts["ref_contrat"]))
        elif mtype == "Entretien individuel":
            if ts.get("periode_evaluee"):
                meta_rows.append(("Période", ts["periode_evaluee"]))
            if ts.get("evaluateur"):
                meta_rows.append(("Évaluateur", ts["evaluateur"]))

        if meta_rows:
            meta_t = doc.add_table(rows=len(meta_rows), cols=2)
            _table_full_width(meta_t)
            _table_no_borders(meta_t)
            for i, (lbl, val) in enumerate(meta_rows):
                cells = meta_t.rows[i].cells
                _cell_margins(cells[0], top=36, bottom=36, left=0, right=80)
                _cell_margins(cells[1], top=36, bottom=36, left=80, right=0)
                r_l = cells[0].paragraphs[0].add_run(lbl)
                r_l.font.size = Pt(9.5)
                r_l.font.bold = True
                r_l.font.color.rgb = _GREY_DARK
                r_l.font.name = "Calibri"
                r_v = cells[1].paragraphs[0].add_run(val)
                r_v.font.size = Pt(9.5)
                r_v.font.color.rgb = theme.primary
                r_v.font.name = "Calibri"

        # ── 7. Quorum CSE (encadré visuel fort) ──────────────────────────────
        if mtype in _CSE_TYPES:
            try:
                presents = int(ts.get("membres_presents") or 0)
                total    = int(ts.get("membres_total") or 0)
                if presents and total:
                    quorum_ok  = presents > total / 2
                    pct        = round(100 * presents / total)
                    quorum_txt = (f"✓  Quorum atteint — {presents}/{total} membres présents ({pct}%)"
                                  if quorum_ok
                                  else f"✗  Quorum NON atteint — {presents}/{total} membres présents ({pct}%)")
                    doc.add_paragraph()
                    q_t = doc.add_table(rows=1, cols=1)
                    _table_full_width(q_t)
                    _table_no_borders(q_t)
                    q_cell = q_t.cell(0, 0)
                    _cell_bg(q_cell, _GREEN if quorum_ok else _rgb(0xFF, 0xEB, 0xEE))
                    _cell_margins(q_cell, top=120, bottom=120, left=200, right=200)
                    p_q = q_cell.paragraphs[0]
                    p_q.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    r_q = p_q.add_run(quorum_txt)
                    r_q.font.size = Pt(10)
                    r_q.font.bold = True
                    r_q.font.color.rgb = _WHITE if quorum_ok else _RED
                    r_q.font.name = "Calibri"
            except (ValueError, TypeError):
                pass

        # ── 8. Pied de couverture ─────────────────────────────────────────────
        doc.add_paragraph()
        doc.add_paragraph()
        p_gen = doc.add_paragraph()
        p_gen.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r_gen = p_gen.add_run(f"Généré par TranscrIA  ▪  {datetime.today().strftime('%d/%m/%Y')}")
        r_gen.font.size = Pt(8.5)
        r_gen.font.color.rgb = _GREY_DARK
        r_gen.font.name = "Calibri"

        if score is not None:
            score_color = _GREEN if score >= 85 else _ORANGE if score >= 65 else _RED
            r_score = p_gen.add_run(f"  ▪  Qualité : {score}/100")
            r_score.font.size = Pt(8.5)
            r_score.font.color.rgb = score_color
            r_score.font.bold = True
            r_score.font.name = "Calibri"

        if theme.cover_badge:
            p_badge = doc.add_paragraph()
            p_badge.alignment = WD_ALIGN_PARAGRAPH.CENTER
            r_badge = p_badge.add_run(f"[ {theme.cover_badge} ]")
            r_badge.font.size = Pt(8)
            r_badge.font.color.rgb = theme.accent
            r_badge.font.bold = True
            r_badge.font.name = "Calibri"
            p_badge.paragraph_format.space_before = Pt(2)

    # ── Helpers section ───────────────────────────────────────────────────────

    def _section_heading(self, doc: Document, number: str, label: str) -> None:
        theme = self.theme
        doc.add_paragraph()
        p = doc.add_paragraph()
        # Numéro en couleur accent
        r_num = p.add_run(f"{number}  ")
        r_num.font.size = Pt(13)
        r_num.font.bold = True
        r_num.font.color.rgb = theme.accent
        r_num.font.name = "Calibri"
        # Libellé en couleur primaire
        r_lbl = p.add_run(label.upper())
        r_lbl.font.size = Pt(13)
        r_lbl.font.bold = True
        r_lbl.font.color.rgb = theme.primary
        r_lbl.font.name = "Calibri"
        # Trait bas coloré
        _para_bottom_border(p, theme.accent, sz=6)

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
        # Priorité : édition manuelle (étape 4) > synthèse harmonisée sur le glossaire
        # validé (post-correction) > synthèse brute de la LLM (pré-correction).
        summary = (
            ctx.get("summary")
            or ctx.get("summary_harmonized")
            or ctx.get("summary_llm")
            or ""
        ).strip()

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
            r.font.color.rgb = self.theme.primary
            r.font.name = "Calibri"
            p_head.paragraph_format.space_after = Pt(4)

            for raw in synth.splitlines():
                line = raw.strip()
                if not line:
                    continue
                # Intertitre markdown (## …) → ligne en gras, légèrement détachée.
                heading = re.match(r"^#{1,6}\s+(.*)$", line)
                if heading:
                    p = doc.add_paragraph()
                    _add_markdown_runs(p, heading.group(1).strip(), size=10.5, bold_all=True)
                    p.paragraph_format.space_before = Pt(6)
                    p.paragraph_format.space_after = Pt(2)
                    continue
                # Puce markdown (- … ou * …).
                bullet = re.match(r"^[-*]\s+(.*)$", line)
                p = doc.add_paragraph()
                if bullet:
                    _add_markdown_runs(p, "•  " + bullet.group(1).strip(), size=10)
                    p.paragraph_format.left_indent = Cm(0.5)
                else:
                    # Paragraphe de prose : le gras **…** (intertitres en début de
                    # paragraphe) est rendu en gras réel au lieu d'être supprimé.
                    _add_markdown_runs(p, line, size=10)
                p.paragraph_format.space_after = Pt(4)

        # Champs utilisateur spécifiques au type (président CSE, nom projet, etc.)
        self._section_type_specific(doc)

    # ── Section 1b : Données type-spécifiques (champs utilisateur) ──────────────

    def _section_type_specific(self, doc: Document) -> None:
        """Affiche les champs saisis par l'utilisateur pour ce type de réunion.

        Absent si aucun champ n'a été rempli.
        Pour CSE : indicateur de quorum calculé automatiquement.
        """
        ts = self.type_specific_data
        mt = self.meeting_type
        if not ts:
            return

        # Filtrer les champs non vides
        non_empty = {k: v for k, v in ts.items() if v is not None and str(v).strip()}
        if not non_empty:
            return

        # Labels par clé
        LABELS: dict[str, str] = {
            "president_seance": "Président de séance",
            "secretaire_seance": "Secrétaire de séance",
            "membres_presents": "Membres présents",
            "membres_total": "Membres total",
            "ref_pv_precedent": "Réf. PV précédent",
            "objet_seance": "Objet de la séance",
            "nom_projet": "Projet",
            "phase_jalon": "Phase / Jalon",
            "chef_de_projet": "Chef de projet",
            "sprint": "Sprint",
            "ordre_du_jour_items": "Ordre du jour",
            "kpis": "KPIs présentés",
            "nom_client": "Client",
            "ref_contrat": "Référence contrat",
            "periode_evaluee": "Période évaluée",
            "poste_evalue": "Poste évalué",
            "evaluateur": "Évaluateur",
            "formateur": "Formateur",
            "nb_participants_formation": "Nb participants",
            "lieu_formation": "Lieu",
            "nature_incident": "Nature incident",
            "responsable_crise": "Responsable crise",
            "thematique": "Thématique",
            "nb_groupes": "Groupes de travail",
            "objet_negociation": "Objet",
            "parties": "Parties prenantes",
        }

        doc.add_paragraph()
        # Tableau compact sans bordures extérieures
        rows_data: list[tuple[str, str]] = []

        for key, val in non_empty.items():
            label = LABELS.get(key, key.replace("_", " ").capitalize())
            # Ordre du jour : chaque ligne → item
            if key == "ordre_du_jour_items":
                for i, line in enumerate(str(val).splitlines()):
                    line = line.strip()
                    if line:
                        rows_data.append((f"ODJ {i+1}" if i == 0 else "", line))
                continue
            rows_data.append((label, str(val).strip()))

        # Quorum CSE calculé
        if mt in ("CSE", "CSE extraordinaire"):
            try:
                presents = int(non_empty.get("membres_presents", 0))
                total    = int(non_empty.get("membres_total", 0))
                if presents and total:
                    pct    = round(100 * presents / total)
                    quorum = "✓ Quorum atteint" if presents > total / 2 else "✗ Quorum non atteint"
                    rows_data.append(("Quorum", f"{quorum} ({presents}/{total} — {pct}%)"))
            except (ValueError, TypeError):
                pass

        if not rows_data:
            return

        table = doc.add_table(rows=len(rows_data), cols=2)
        _table_full_width(table)
        _table_no_borders(table)

        for i, (label, val) in enumerate(rows_data):
            cells = table.rows[i].cells
            _cell_margins(cells[0], top=30, bottom=30, left=0, right=60)
            _cell_margins(cells[1], top=30, bottom=30, left=60, right=0)

            r_lbl = cells[0].paragraphs[0].add_run(label)
            r_lbl.font.size = Pt(9.5)
            r_lbl.font.bold = True
            r_lbl.font.color.rgb = _GREY_DARK
            r_lbl.font.name = "Calibri"

            color = _GREEN if "Quorum atteint" in val else _RED if "non atteint" in val else self.theme.primary
            r_val = cells[1].paragraphs[0].add_run(val)
            r_val.font.size = Pt(9.5)
            r_val.font.color.rgb = color
            r_val.font.name = "Calibri"

    # ── Section 1c : Données enrichies LLM (décisions, actions, votes…) ─────────

    def _section_enriched(self, doc: Document) -> None:
        """Sections issues de l'extraction LLM structurée.

        Principe : **une donnée extraite n'est jamais cachée**. Toute section
        s'affiche dès qu'elle contient des éléments, quel que soit le type de
        réunion. Le type pilote uniquement le thème visuel et les champs de
        saisie (`type_specific_data`), pas la rétention du contenu extrait.

        Ordre fixe inspiré d'un procès-verbal : agenda → décisions → votes →
        résolutions → actions → blocages → reports. L'absence totale est
        silencieuse (aucun placeholder).
        """
        sd = self.structured_data
        section_num = 2  # numéro de section courant avant Participants

        # (label, items) dans l'ordre PV — chaque section affichée si non vide
        ordered = [
            ("Ordre du jour",        sd.get("points_odj")),
            ("Décisions prises",     sd.get("decisions")),
            ("Votes",                sd.get("votes")),
            ("Résolutions adoptées", sd.get("resolutions")),
            ("Actions à réaliser",   sd.get("actions")),
            ("Points bloquants",     sd.get("blocages")),
            ("Points reportés",      sd.get("reports")),
        ]
        shown: list[tuple[str, list[str]]] = [
            (label, items) for label, items in ordered if items
        ]

        if not shown:
            return 0

        for label, items in shown:
            self._section_heading(doc, f"{section_num}.", label)
            section_num += 1
            for item in items:
                p = doc.add_paragraph()
                p.paragraph_format.left_indent = Cm(0.5)
                p.paragraph_format.space_after = Pt(2)
                run_bullet = p.add_run("▸  ")
                run_bullet.font.color.rgb = self.theme.accent
                run_bullet.font.size = Pt(9)
                run = p.add_run(item)
                run.font.size = Pt(10)
                run.font.name = "Calibri"

        # Prochaine date — footer discret si mentionnée
        if sd.get("prochaine_date"):
            doc.add_paragraph()
            p = doc.add_paragraph()
            r_lbl = p.add_run("Prochaine réunion : ")
            r_lbl.font.size = Pt(9)
            r_lbl.font.bold = True
            r_lbl.font.color.rgb = _GREY_DARK
            r_lbl.font.name = "Calibri"
            r_val = p.add_run(sd["prochaine_date"])
            r_val.font.size = Pt(9)
            r_val.font.color.rgb = self.theme.accent
            r_val.font.name = "Calibri"

        return len(shown)

    # ── Section N : Participants ──────────────────────────────────────────────

    def _section_participants(self, doc: Document, base: int = 2) -> None:
        self._section_heading(doc, f"{base}.", "Participants & Locuteurs")

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
            _cell_bg(hdr_cells[i], self.theme.primary)
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
            bg = self.theme.light if row_i % 2 == 0 else _WHITE

            col = 0
            data: list[tuple[str, bool, RGBColor]] = []  # (text, bold, color)

            data.append((participant["name"], True, self.theme.primary))
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
                data.append(("★ Animateur" if participant["is_animator"] else "", True, self.theme.accent))

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

    def _section_transcript(self, doc: Document, base: int = 3) -> None:
        self._section_heading(doc, f"{base}.", "Transcription")

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
            r1.font.color.rgb = self.theme.accent if entry["speaker"] else _GREY_DARK
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

    def _section_quality(self, doc: Document, base: int = 4) -> None:
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

        self._section_heading(doc, f"{base}.", "Points à vérifier")

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
        theme  = self.theme
        title  = (self.ctx.get("title") or "TranscrIA")[:40]
        date   = _fmt_date(self.ctx.get("date", ""))
        score  = self.quality.get("quality_score")

        section = doc.sections[0]
        footer  = section.footer
        footer.is_linked_to_previous = False
        p = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
        p.clear()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER

        # Pastille score qualité
        if score is not None:
            score_color = _GREEN if score >= 85 else _ORANGE if score >= 65 else _RED
            r_score = p.add_run(f"■ {score}/100   ")
            r_score.font.size = Pt(7.5)
            r_score.font.color.rgb = score_color
            r_score.font.name = "Calibri"

        # Trait couleur theme avant le titre
        r_accent = p.add_run("▪ ")
        r_accent.font.size = Pt(7.5)
        r_accent.font.color.rgb = theme.accent
        r_accent.font.name = "Calibri"

        r_info = p.add_run(f"TranscrIA  ·  {title}  ·  {date}  ·  Page ")
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


_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")


def _split_markdown_bold(text: str) -> list[tuple[str, bool]]:
    """Découpe un texte en segments ``(contenu, gras)`` selon le markdown `**..**`/`__..__`.

    Les segments vides sont écartés. Un texte sans marqueur renvoie un unique
    segment non gras. Sert à rendre le gras de la LLM dans le DOCX au lieu de
    simplement retirer les astérisques.
    """
    segments: list[tuple[str, bool]] = []
    pos = 0
    for match in _MD_BOLD_RE.finditer(text):
        if match.start() > pos:
            segments.append((text[pos:match.start()], False))
        segments.append((match.group(1) or match.group(2) or "", True))
        pos = match.end()
    if pos < len(text):
        segments.append((text[pos:], False))
    return [(content, bold) for content, bold in segments if content]


def _add_markdown_runs(
    paragraph: Any,
    text: str,
    *,
    size: float = 10.0,
    name: str = "Calibri",
    bold_all: bool = False,
) -> None:
    """Ajoute ``text`` au paragraphe en rendant le gras markdown (`**`/`__`) en runs gras."""
    for content, is_bold in _split_markdown_bold(text):
        run = paragraph.add_run(content)
        run.font.size = Pt(size)
        run.font.name = name
        run.font.bold = bool(is_bold or bold_all)


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
    structured_data = ctx.get("structured_data") or {}

    srt_text = fs.load_text("metadata/transcription_corrigee.srt") or ""
    if not srt_text:
        srt_text = fs.load_text("metadata/transcription.srt") or ""

    report = DocxReport(ctx, participants, speaker_stats, quality, srt_text, structured_data)
    doc = report.build()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    return output_path

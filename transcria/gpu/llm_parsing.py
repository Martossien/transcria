"""Parseurs des réponses LLM (vague C2) — fonctions texte→objets, zéro I/O.

Corps extraits d'``OpenCodeRunner`` : parsing du résumé structuré (champs,
participants, termes suspects, données structurées) et lecture des métriques
vLLM. Aucun sous-processus, aucun fichier — testables sans mock de processus.
``OpenCodeRunner`` conserve des délégateurs ``staticmethod`` : les appelants
historiques et les tests passent par la classe.
"""
import json
import logging
import re

from transcria.gpu.prompt_locator import summary_markers

logger = logging.getLogger(__name__)

_STRUCTURED_DATA_EMPTY: dict = {
    "decisions": [], "actions": [], "blocages": [], "reports": [],
    "votes": [], "resolutions": [], "points_odj": [], "prochaine_date": "",
}


def normalize_summary_variants(value, term: str = "") -> list[str]:
    if isinstance(value, list):
        candidates = value
    elif isinstance(value, str):
        candidates = re.split(r'\s*[;,]\s*', value)
    else:
        candidates = []

    normalized = []
    seen = set()
    term_key = term.strip().casefold()
    empty_markers = {"aucun", "aucune", "(aucun)", "(aucune)", "néant", "neant", "n/a", "na", "-"}
    for candidate in candidates:
        text = str(candidate).strip()
        key = text.casefold()
        if not text or key in empty_markers:
            continue
        if term_key and key == term_key:
            continue
        if key in seen:
            continue
        seen.add(key)
        normalized.append(text)
    return normalized


def strip_summary_context_wrappers(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^`(.+)`$", r"\1", text).strip()
    pairs = {
        '"': '"',
        "'": "'",
        "«": "»",
        "“": "”",
        "‘": "’",
    }
    while len(text) >= 2 and pairs.get(text[0]) == text[-1]:
        text = text[1:-1].strip()
    return text


def clean_summary_context_quote(value: str) -> str:
    text = strip_summary_context_wrappers(value)
    text = text.strip().strip("|").strip()
    text = strip_summary_context_wrappers(text)
    return text[:500].strip()


def parse_summary_contexts(value: str) -> list[dict]:
    contexts: list[dict] = []
    if not value:
        return contexts
    chunks = [chunk.strip() for chunk in re.split(r'\s*\|\|\s*|\s*;\s*(?=["«“]?\[?[^\]]+\])', value) if chunk.strip()]
    if len(chunks) == 1 and " ; " in value:
        chunks = [chunk.strip() for chunk in value.split(" ; ") if chunk.strip()]
    timestamp = r"(?:\d+(?:[\.,]\d+)?s|\d{1,3}:\d{2}(?::\d{2})?(?:[\.,]\d+)?s?)"
    time_range = rf"{timestamp}(?:\s*(?:→|->|-)\s*{timestamp})?"
    for chunk in chunks[:3]:
        text = strip_summary_context_wrappers(chunk)
        match = re.match(
            rf'^[«"“]?\[?(?P<timecode>{time_range})\]?[»"”]?\s*'
            rf'(?:(?P<speaker>SPEAKER_[A-Za-z0-9]+)\s*:\s*)?'
            rf'(?P<quote>.+?)(?:\s*\((?P<reason>.+)\))?$',
            text,
        )
        if match:
            quote = clean_summary_context_quote(match.group("quote") or "")
            contexts.append({
                "variant": "",
                "timecode": match.group("timecode").strip(),
                "speaker": (match.group("speaker") or "").strip(),
                "quote": quote,
                "reason": (match.group("reason") or "").strip(),
            })
        else:
            cleaned = text.strip("[] ")
            if cleaned:
                contexts.append({
                    "variant": "",
                    "timecode": "",
                    "speaker": "",
                    "quote": clean_summary_context_quote(cleaned),
                    "reason": "",
                })
    return contexts


def clean_summary_cell(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^\s*[-*•]\s*", "", text)
    text = re.sub(r"^\s*\d+[\.)]\s*", "", text)
    text = text.strip().strip("|").strip()
    text = re.sub(r"\*\*\s+\*\*", " ", text)
    text = re.sub(r"^`(.+)`$", r"\1", text)
    text = re.sub(r"^\*\*(.+)\*\*$", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def split_markdown_table_row(line: str) -> list[str]:
    if "|" not in line:
        return []
    cells = [clean_summary_cell(cell) for cell in line.strip().strip("|").split("|")]
    return cells


def summary_section(text: str, heading_re: str) -> tuple[str, bool]:
    match = re.search(
        rf"^\s*##+\s+{heading_re}[ \t]*\n(?P<body>.*?)(?=^\s*##+\s+|\Z)",
        text,
        re.DOTALL | re.IGNORECASE | re.MULTILINE,
    )
    if not match:
        return "", False
    return match.group("body").strip(), True


def normalize_summary_lines(section: str) -> list[str]:
    lines: list[str] = []
    current = ""
    for raw in section.splitlines():
        line = raw.strip()
        if not line:
            continue
        marker_text = line.replace("|", "").replace(":", "").replace("-", "").strip()
        if not marker_text:
            continue
        starts_entry = bool(
            line.startswith(("-", "*", "•", "|"))
            or line[:2].isdigit()
            or line.startswith("**")
        )
        if starts_entry:
            if current:
                lines.append(current.strip())
            current = line
        elif current:
            current += " " + line
        else:
            current = line
    if current:
        lines.append(current.strip())
    return lines


def extract_summary_field(text: str, names: tuple[str, ...]) -> str:
    for name in names:
        match = re.search(
            rf"(?:^|\|)\s*{re.escape(name)}\s*:\s*(.+?)(?=\s*\|\s*[\wÀ-ÿ _/-]+\s*:|\s*$)",
            text,
            re.IGNORECASE,
        )
        if match:
            return clean_summary_cell(match.group(1))
    return ""


def parse_summary_term_line(line: str, table_headers: list[str] | None = None) -> dict | None:
    text = line.strip()
    if not text:
        return None
    lowered = text.casefold()
    if "non identifiable" in lowered or "aucun terme suspect" in lowered or lowered in {"(aucun)", "(aucune)"}:
        return None
    has_term_shape = (
        text.startswith(("-", "*", "•", "|"))
        or "**" in text
        or "|" in text
        or ("[" in text and "]" in text)
        or re.match(r"^\s*\d+[\.)]\s+", text) is not None
    )
    if not has_term_shape:
        return None

    if table_headers and text.startswith("|"):
        cells = split_markdown_table_row(text)
        if len(cells) >= len(table_headers):
            values = {table_headers[i]: cells[i] for i in range(min(len(table_headers), len(cells)))}
            term = values.get("term") or values.get("terme") or values.get("forme") or values.get("forme validée") or ""
            category = values.get("catégorie") or values.get("categorie") or values.get("category") or "mot suspect"
            priority = values.get("priorité") or values.get("priorite") or values.get("priority") or "normale"
            variants_raw = values.get("variantes") or values.get("variantes suspectes") or values.get("variants") or ""
            comment = values.get("commentaire") or values.get("justification") or values.get("comment") or ""
            contexts_raw = values.get("contextes") or values.get("contexte") or values.get("contexts") or ""
            source_raw = values.get("source") or values.get("provenance") or ""
            term = clean_summary_cell(term)
            if term and "terme" not in term.casefold():
                return {
                    "term": term,
                    "category": clean_summary_cell(category) or "mot suspect",
                    "priority": clean_summary_cell(priority) or "normale",
                    "variants": normalize_summary_variants(variants_raw, term=term),
                    "comment": clean_summary_cell(comment),
                    "contexts": parse_summary_contexts(contexts_raw),
                    "source": normalize_summary_source(source_raw),
                }

    text = re.sub(r"^\s*[-*•]\s*", "", text).strip()
    text = re.sub(r"^\s*\d+[\.)]\s*", "", text).strip()

    term_match = re.match(
        r"(?:\*\*)?(?P<term>.+?)(?:\*\*)?\s*(?:\[(?P<category>[^\]]*)\])?\s*(?:\((?P<priority>[^)]*)\))?(?P<suffix>\s*(?:[:|].*)?)$",
        text,
    )
    if not term_match:
        return None

    raw_term = clean_summary_cell(term_match.group("term"))
    raw_term = re.sub(r"\s*\|\s*$", "", raw_term).strip()
    suffix = (term_match.group("suffix") or "").strip()
    category = clean_summary_cell(term_match.group("category") or "")
    priority = clean_summary_cell(term_match.group("priority") or "")

    if not raw_term:
        return None

    inline_category = extract_summary_field(suffix, ("catégorie", "categorie", "category"))
    inline_priority = extract_summary_field(suffix, ("priorité", "priorite", "priority"))
    variants_raw = extract_summary_field(
        suffix,
        ("variantes_suspectes", "variantes suspectes", "variantes", "variants"),
    )
    comment = extract_summary_field(suffix, ("commentaire", "justification", "comment"))
    contexts_raw = extract_summary_field(suffix, ("contextes", "contexte", "contexts"))
    source_raw = extract_summary_field(suffix, ("source", "provenance"))

    if inline_category:
        category = inline_category
    if inline_priority:
        priority = inline_priority
    if not comment and suffix.startswith(":"):
        comment = clean_summary_cell(suffix[1:])

    term = raw_term
    variants = normalize_summary_variants(variants_raw, term=term)
    if not variants and "/" in raw_term:
        parts = [clean_summary_cell(p) for p in raw_term.split("/") if p.strip()]
        if parts:
            term = parts[0]
            variants = normalize_summary_variants(parts[1:], term=term)
            if not comment:
                comment = f"Variantes suspectes détectées par la LLM : {raw_term}"

    return {
        "term": term,
        "category": category or "mot suspect",
        "priority": priority or "normale",
        "variants": variants,
        "comment": comment,
        "contexts": parse_summary_contexts(contexts_raw),
        "source": normalize_summary_source(source_raw),
    }


def normalize_summary_source(raw: str) -> str:
    """Provenance d'un terme suspect. Seule « document » (recoupement avec les
    documents présentés, cf. summary_prompt §6.10) est reconnue ; sinon chaîne vide."""
    return "document" if raw and "document" in raw.casefold() else ""


def vllm_metrics_busy(metrics_text: str) -> bool | None:
    """Lit l'activité vLLM dans la sortie Prometheus ``/metrics`` : occupé si des
    requêtes tournent OU attendent. None si les compteurs sont absents (pas du vLLM)."""
    seen = False
    busy = False
    for name in ("vllm:num_requests_running", "vllm:num_requests_waiting"):
        for m in re.finditer(rf"^{re.escape(name)}(?:\{{[^}}]*\}})?\s+([0-9eE.+-]+)\s*$",
                             metrics_text, re.MULTILINE):
            seen = True
            try:
                if float(m.group(1)) > 0:
                    busy = True
            except ValueError:
                continue
    return busy if seen else None


def parse_participant_line(line: str) -> tuple[str | None, str, str]:
    """Extrait speaker_id, label et rôle depuis une ligne Participants probables."""
    text = line.strip("- ").strip()
    if not text:
        return None, "", ""

    match = re.match(r"^(SPEAKER_\d+)\s+\[([^\]]+)\]\s*:\s*(.+)$", text)
    if match:
        return match.group(1), match.group(2).strip(), match.group(3).strip()

    match = re.match(r"^(SPEAKER_\d+)\s*:\s*(.+)$", text)
    if match:
        speaker_id = match.group(1)
        rest = match.group(2).strip()
        split = re.split(r"\s+[—–-]\s+", rest, maxsplit=1)
        if len(split) == 2:
            return speaker_id, split[0].strip(), split[1].strip()
        return speaker_id, "", rest

    return None, "", ""


def strip_role_gender(text: str) -> str:
    """Retire tout marqueur de genre vocal (indice acoustique) d'une ligne participant.

    Le genre a un champ dédié (stats/DOCX) et ne doit jamais polluer le rôle
    (garde ``role_gender_clean``). La LLM le recopie parfois AILLEURS qu'en fin
    de ligne (dans le label, entre parenthèses, au milieu) — l'ancienne version,
    ancrée en fin, le ratait. On retire donc, OÙ QU'ILS SOIENT : les symboles
    ♂/♀ ; « voix/genre/sexe masculin·e|féminin·e » ; un genre entre parenthèses ;
    un genre détaché par une ponctuation de séparation (— – - , ; /). En FIN de
    ligne uniquement, on retire aussi un « masculin/féminin/homme/femme » isolé
    (l'indice recopié après le rôle). Un genre simplement accolé à un nom
    (« vestiaire masculin », « équipe féminine ») est CONSERVÉ : ce n'est pas
    l'indice de voix. Les artefacts (parenthèses/crochets vides, séparateurs
    orphelins, doubles espaces) sont nettoyés ; le point final est préservé.
    """
    # Adjectif de genre accordé (masculin·e / féminin·e) + symbole facultatif.
    g = r"(?:masculin|f[ée]minin)e?\s*[♂♀]?"
    cleaned = text
    # (a) « (…genre…) » : parenthèse ne contenant qu'un marqueur de genre.
    cleaned = re.sub(rf"\(\s*(?:voix\s+|genre\s+|sexe\s+)?{g}\s*\)", "", cleaned, flags=re.IGNORECASE)
    # (b) « voix/genre/sexe masculin·e|féminin·e » n'importe où.
    cleaned = re.sub(rf"\b(?:voix|genre|sexe)\s+{g}", "", cleaned, flags=re.IGNORECASE)
    # (c) genre détaché par une ponctuation de séparation, n'importe où ; on consomme
    #     aussi la virgule/point-virgule fermante d'un appositif (« A, masculin, B » → « A B »).
    cleaned = re.sub(rf"\s*[—–\-,;/]\s*{g}\s*[,;]?", "", cleaned, flags=re.IGNORECASE)
    # (d) symbole isolé n'importe où.
    cleaned = re.sub(r"\s*[♂♀]", "", cleaned)
    # (e1) indice isolé en FIN de ligne précédé d'une ponctuation de séparation
    #      (— – - ( , ; /), quelle que soit la casse.
    cleaned = re.sub(
        r"[\s]*[—–\-(,;/][\s]*\b(?:masculin|f[ée]minin|homme|femme)\b\s*[♂♀]?\s*\)?\s*$",
        "", cleaned, flags=re.IGNORECASE,
    )
    # (e2) indice en fin de ligne précédé d'un simple espace : retiré UNIQUEMENT s'il est
    #      capitalisé (« Masculin/Féminin » = l'indice recopié). Un adjectif légitime
    #      accolé et lowercase (« vestiaire masculin », « foot féminin ») est PRÉSERVÉ
    #      (bug pré-0.3.0 : l'ancienne règle mangeait ce dernier mot). Sensible à la casse.
    cleaned = re.sub(
        r"\s+\b(?:Masculin|F[ée]minin|Homme|Femme)\b\s*[♂♀]?\s*\)?\s*$",
        "", cleaned,
    )
    # Nettoyage des artefacts laissés par les retraits.
    cleaned = re.sub(r"\(\s*\)", "", cleaned)                # parenthèses vides
    cleaned = re.sub(r"[\s,;/]+([)\]])", r"\1", cleaned)     # séparateur avant fermeture
    cleaned = re.sub(r"([,;/])\s*(?:[,;/]\s*)+", r"\1 ", cleaned)  # séparateurs redondants
    cleaned = re.sub(r"\s+([.,;:)\]])", r"\1", cleaned)      # espace avant ponctuation
    cleaned = re.sub(r"\s{2,}", " ", cleaned)                # doubles espaces
    cleaned = re.sub(r"[\s—–\-,;/]+$", "", cleaned)          # séparateur orphelin en fin (garde le point)
    return cleaned.strip()


def is_non_identifiable_participant_line(line: str) -> bool:
    """Détecte une vraie ligne placeholder, sans rejeter un rôle contenant ces mots."""
    text = clean_summary_cell(line.strip("- ").strip())
    if not text:
        return True
    lowered = text.casefold()
    # Sentinelles « aucun participant » FR + EN (Axe B : le prompt EN écrit "(not identifiable)").
    _none = {"non identifiable", "(non identifiable)", "aucun", "(aucun)", "aucune", "(aucune)",
             "not identifiable", "(not identifiable)", "none", "(none)"}
    if lowered in _none:
        return True

    speaker_id, label, role = parse_participant_line(text)
    if speaker_id:
        label_key = label.casefold().strip()
        role_key = role.casefold().strip()
        _lbl_none = {"non identifiable", "(non identifiable)", "not identifiable", "(not identifiable)"}
        return label_key in _lbl_none or (not label_key and role_key in _lbl_none)

    return lowered.startswith(("non identifiable", "not identifiable"))


def normalize_structured_data(raw: dict, extra_keys: tuple[str, ...] = ()) -> dict:
    """Normalise le dict brut extrait du JSON LLM en structure canonique.

    ``extra_keys`` = clés d'extraction déclarées par le type de réunion choisi
    (fiche personnalisée) — normalisées comme les listes universelles, jamais
    conservées brutes (contrat « listes de chaînes » du DOCX et de l'UI).
    """
    result = dict(_STRUCTURED_DATA_EMPTY)
    for field in ("decisions", "actions", "blocages", "reports", "votes", "resolutions", "points_odj", *extra_keys):
        val = raw.get(field)
        if isinstance(val, list):
            result[field] = [str(item).strip() for item in val if str(item).strip()]
        elif isinstance(val, str) and val.strip():
            result[field] = [val.strip()]
    date_val = raw.get("prochaine_date", "")
    result["prochaine_date"] = str(date_val).strip() if date_val else ""
    return result


def parse_structured_data(
    text: str, extra_keys: tuple[str, ...] = (), language: str = "fr"
) -> tuple[dict, str, str]:
    """Extrait la section « données structurées » du markdown LLM (en-tête selon ``language``).

    Trois niveaux de fallback :
      1. json.loads() strict → status "ok"
      2. Regex champ par champ → status "partial"
      3. Échec total → status "failed"
    Si la section est absente → status "missing"

    Returns:
        (data_dict, parse_status, parse_warning)
    """
    EMPTY = dict(_STRUCTURED_DATA_EMPTY)

    section, has_section = summary_section(text, summary_markers(language)["structured_section_re"])
    if not has_section:
        logger.debug("_parse_structured_data: section absente")
        return EMPTY, "missing", ""

    # Extraire le contenu du bloc ```json ... ``` ou toute la section
    code_match = re.search(r"```(?:json)?\s*\n(.*?)\n```", section, re.DOTALL)
    json_text = code_match.group(1).strip() if code_match else section.strip()

    # Niveau 1 : json.loads strict
    try:
        raw = json.loads(json_text)
        if isinstance(raw, dict):
            data = normalize_structured_data(raw, extra_keys)
            non_empty = sum(1 for v in data.values() if v)
            logger.debug("_parse_structured_data: ok — %d champs non vides", non_empty)
            return data, "ok", ""
    except (ValueError, TypeError):
        pass

    # Niveau 2 : regex champ par champ
    data = EMPTY.copy()
    failed_fields: list[str] = []
    extracted_any = False

    for field in ("decisions", "actions", "blocages", "reports", "votes", "resolutions", "points_odj", *extra_keys):
        m = re.search(rf'"{field}"\s*:\s*\[([^\]]*)\]', json_text, re.DOTALL)
        if m:
            items = re.findall(r'"([^"]{2,})"', m.group(1))
            data[field] = [i.strip() for i in items if i.strip()]
            if data[field]:
                extracted_any = True
        else:
            failed_fields.append(field)

    dm = re.search(r'"prochaine_date"\s*:\s*"([^"]*)"', json_text)
    if dm:
        data["prochaine_date"] = dm.group(1)

    if extracted_any:
        warning = (
            f"JSON malformé — extraction partielle, champs non extraits : {', '.join(failed_fields)}"
            if failed_fields else "JSON malformé — extraction partielle"
        )
        logger.warning("_parse_structured_data: partial — %s", warning)
        return data, "partial", warning

    # Niveau 3 : échec total
    warning = "Section ## Données structurées présente mais JSON non parseable"
    logger.warning("_parse_structured_data: failed — réponse LLM inattendue dans section données structurées")
    return EMPTY, "failed", warning


def parse_structured_summary(
    text: str, extra_structured_keys: tuple[str, ...] = (), language: str = "fr"
) -> dict:
    """Parse le markdown structuré en dictionnaire de champs.

    ``language`` : langue des marqueurs de sortie (Axe B). ``fr`` = comportement
    historique inchangé ; ``en`` = marqueurs anglais (cf. ``summary_markers``)."""
    m = summary_markers(language)
    fields = {
        "title_suggere": "",
        "type_suggere": "",
        "sujet_suggere": "",
        "objectif_suggere": "",
        "notes_suggeres": "",
        "participants_detectes": "",
        "mots_cles": "",
        "speaker_count": 0,
    }

    patterns = {
        "title_suggere": rf"\*\*{m['title']}\s*:\s*\*\*\s*(.+?)(?:\n|$)",
        "type_suggere": rf"\*\*{m['type']}\s*:\s*\*\*\s*(.+?)(?:\n|$)",
        "sujet_suggere": rf"\*\*{m['subject']}\s*:\s*\*\*\s*(.+?)(?:\n|$)",
        "objectif_suggere": rf"\*\*{m['objective']}\s*:\s*\*\*\s*(.+?)(?:\n|$)",
        "notes_suggeres": rf"\*\*{m['notes']}\s*:\s*\*\*\s*(.+?)(?:\n|$)",
        "mots_cles": rf"\*\*{m['keywords']}\*\*\s*\n(.+?)(?:\n\n|\Z)",
    }

    for key, pattern in patterns.items():
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if match:
            value = match.group(1).strip()
            if key == "mots_cles":
                value = value.replace("\n", " ").strip()
            fields[key] = value

    missing_critical = [k for k in ("title_suggere", "type_suggere", "sujet_suggere") if not fields[k]]
    if missing_critical:
        logger.warning("_parse_structured_summary: champs critiques non extraits — %s", missing_critical)

    nb_match = re.search(rf"\*\*{m['participant_count']}\s*:\s*\*\*\s*(\d+)", text)
    if nb_match:
        fields["speaker_count"] = int(nb_match.group(1))

    part_match = re.search(rf"{re.escape(m['participants_heading'])}\s*\n(.+?)(?:\n##|\Z)", text, re.DOTALL)
    if not part_match:
        logger.warning("_parse_structured_summary: section '%s' introuvable", m['participants_heading'])
    if part_match:
        participants = []
        speaker_roles: dict[str, dict] = {}
        for line in part_match.group(1).strip().split("\n"):
            line = line.strip("- ").strip()
            line = strip_role_gender(line)
            if is_non_identifiable_participant_line(line):
                continue
            participants.append(line)
            # Extraire SPEAKER_XX + label + rôle.
            # Formats acceptés :
            # - "SPEAKER_XX [label] : rôle"
            # - "SPEAKER_XX : label — rôle"
            # - "SPEAKER_XX : rôle" (sans label)
            speaker_id, label, role = parse_participant_line(line)
            if speaker_id and role:
                speaker_roles[speaker_id] = {"label": label, "role": role}
        fields["participants_detectes"] = "\n".join(participants)
        if speaker_roles:
            fields["speaker_roles"] = speaker_roles

    # Parse lexicon pre-fill sections. Keep old headings for compatibility with
    # summaries produced before the prompt was narrowed to "termes douteux".
    termes_suspects = []
    terms_section, has_terms_section = summary_section(
        text,
        m["terms_section_re"],
    )
    parse_status = "missing"
    parse_warning = ""
    if not has_terms_section:
        logger.warning("_parse_structured_summary: section termes ('%s') introuvable", m["terms_section_re"])
    else:
        table_headers: list[str] | None = None
        for line in normalize_summary_lines(terms_section):
            if line.startswith("|"):
                cells = split_markdown_table_row(line)
                normalized_cells = [c.casefold() for c in cells]
                if any(c in {"terme", "term", "forme", "forme validée"} for c in normalized_cells):
                    table_headers = normalized_cells
                    continue
                if all(not c.replace("-", "").replace(":", "").strip() for c in cells):
                    continue

            parsed_term = parse_summary_term_line(line, table_headers)
            if parsed_term is None:
                continue
            termes_suspects.append(parsed_term)
    if has_terms_section and not termes_suspects:
        _low = terms_section.casefold()
        _empty = "aucun terme suspect" in _low or "no doubtful term" in _low or "no suspect term" in _low
        parse_status = "empty" if _empty else "section_unparsed"
        if parse_status == "section_unparsed":
            parse_warning = "section termes présente mais aucun terme extrait"
            logger.warning("_parse_structured_summary: section termes présente mais aucun terme extrait (format inattendu ?)")
    else:
        parse_status = "extracted" if termes_suspects else parse_status
        logger.debug("_parse_structured_summary: %d termes suspects extraits", len(termes_suspects))
    fields["termes_suspects"] = termes_suspects
    fields["termes_suspects_parse_status"] = parse_status
    fields["termes_suspects_parse_warning"] = parse_warning

    sd, sd_status, sd_warning = parse_structured_data(text, extra_structured_keys, language)
    fields["structured_data"] = sd
    fields["structured_data_parse_status"] = sd_status
    fields["structured_data_parse_warning"] = sd_warning

    return fields

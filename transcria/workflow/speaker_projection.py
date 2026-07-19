"""Projection locuteurs — service PUR (vague B1, étape 2).

Le domaine complet extrait de ``WorkflowRunner`` : fusion des suggestions LLM dans le
contexte de réunion, application des rôles/labels aux participants et aux statistiques
locuteurs, attribution acoustique du genre, rendu du contexte de diarisation.

Contrat du module : **entrées = structures chargées, sorties = objets/contenus.**
Il ne connaît ni ``JobFilesystem`` ni le store — les lectures/écritures des fichiers
(``meeting_context.json``, ``participants.json``, ``speaker_stats.json``,
``speaker_mapping.json``, ``summary/*.md``) restent dans la phase appelante.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

# Champs de suggestion LLM projetés tels quels dans meeting_context.json.
SUGGESTION_FIELDS = (
    "title_suggere", "type_suggere", "sujet_suggere",
    "objectif_suggere", "notes_suggeres", "participants_detectes",
)


def truncate_at_word(text: str, max_chars: int = 120) -> str:
    """Coupe à max_chars caractères en respectant la frontière de mot la plus proche."""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars].rsplit(" ", 1)
    return (cut[0] if len(cut) > 1 else text[:max_chars]) + "…"


def normalize_speaker_role_info(info: dict) -> dict:
    """Normalise les anciens formats où le label était inclus dans le rôle."""
    label = str(info.get("label", "") or "").strip()
    role = str(info.get("role", "") or "").strip()
    if not label and role:
        split = re.split(r"\s+[—–-]\s+", role, maxsplit=1)
        if len(split) == 2 and split[0].strip() and split[1].strip():
            label = split[0].strip()
            role = split[1].strip()
    return {"label": label, "role": role}


def merge_llm_suggestions(meeting_ctx: dict, parsed: dict) -> list[str]:
    """Projette les suggestions LLM parsées dans ``meeting_ctx`` (mutation en place).

    Retourne la liste des champs de suggestion NON renseignés par la LLM (à journaliser
    par l'appelant). Ne touche jamais un choix explicite déjà posé par l'utilisateur
    (``language``).
    """
    # Langue des livrables RÉSOLUE (owner.locale / détection) : persistée pour que l'affichage
    # (extraction de la synthèse, en-tête d'extrait, rapports, DOCX) choisisse les bons
    # marqueurs. Ne PAS écraser un choix explicite déjà posé par l'utilisateur.
    if parsed.get("language") and not meeting_ctx.get("language"):
        meeting_ctx["language"] = parsed["language"]

    for fld in SUGGESTION_FIELDS:
        if parsed.get(fld):
            meeting_ctx[fld] = parsed[fld]
    empty_fields = [f for f in SUGGESTION_FIELDS if not parsed.get(f)]

    if parsed.get("speaker_count", 0) > 0:
        meeting_ctx["speaker_count_llm"] = parsed["speaker_count"]
    termes_suspects = parsed.get("termes_suspects") or []
    meeting_ctx["termes_suspects"] = termes_suspects
    meeting_ctx["termes_suspects_parse_status"] = parsed.get("termes_suspects_parse_status", "missing")
    parse_warning = parsed.get("termes_suspects_parse_warning", "")
    if parse_warning:
        meeting_ctx["termes_suspects_parse_warning"] = parse_warning
    else:
        meeting_ctx.pop("termes_suspects_parse_warning", None)

    meeting_ctx["summary_llm"] = parsed.get("summary_text", "")

    # Données structurées enrichies (décisions, actions, votes...)
    sd = parsed.get("structured_data") or {}
    meeting_ctx["structured_data"] = sd
    meeting_ctx["structured_data_parse_status"] = parsed.get("structured_data_parse_status", "missing")
    sd_warning = parsed.get("structured_data_parse_warning", "")
    if sd_warning:
        meeting_ctx["structured_data_parse_warning"] = sd_warning
    else:
        meeting_ctx.pop("structured_data_parse_warning", None)

    # Stocker les rôles LLM dans meeting_context pour que l'UI puisse les afficher
    # et qu'ils puissent être réappliqués après la création du mapping
    speaker_roles = parsed.get("speaker_roles", {})
    if speaker_roles:
        meeting_ctx["speaker_roles_llm"] = speaker_roles
    return empty_fields


def substitute_speaker_names(text: str, mapping: dict | None) -> str:
    """Remplace les jetons SPEAKER_XX d'un texte LIBRE (synthèse, notes LLM) par les
    noms VALIDÉS par l'utilisateur — au RENDU uniquement, l'artefact stocké reste
    intact (fonctionne donc aussi pour les jobs existants).

    Vécu 2026-07-19 : la synthèse est rédigée par la LLM au résumé (autostart),
    AVANT la validation des locuteurs — le DOCX parlait de « SPEAKER_00 » alors que
    les noms étaient connus. Prudence : seuls les jetons EXACTS dont le mapping
    fournit un nom non vide et différent du jeton sont substitués."""
    if not text or not mapping:
        return text
    entries = mapping.get("mapping", mapping) if isinstance(mapping, dict) else {}
    names: dict[str, str] = {}
    for token, info in (entries or {}).items():
        name = str((info or {}).get("name") or "").strip() if isinstance(info, dict) else str(info or "").strip()
        if name and name != token and not name.startswith("SPEAKER_"):
            names[str(token)] = name
    if not names:
        return text
    import re as _re

    return _re.sub(
        r"\bSPEAKER_\d{2}\b",
        lambda m: names.get(m.group(0), m.group(0)),
        text,
    )


def render_summary_markdown(summary_text: str, transcript_short: str, language: str | None) -> str:
    """Contenu de ``summary/summary.md`` : résumé LLM + extrait de transcription.

    summary_text commence déjà par « # Résumé de contrôle » (écrit par opencode) —
    on n'ajoute que la section transcript en fin de fichier, avec l'en-tête localisé
    selon la langue des livrables (Axe B).
    """
    excerpt_heading = "## Transcript excerpt" if language == "en" else "## Extrait de transcription"
    return summary_text + (
        f"\n\n---\n\n{excerpt_heading}\n\n{transcript_short}\n" if transcript_short else "\n"
    )


@dataclass
class SpeakerRolesProjection:
    """Issue de ``apply_speaker_roles`` : structures projetées + quoi persister.

    L'appelant sauve ``participants`` si ``updated or created``, ``spk_stats`` si
    ``propagated``, et le mapping si ``mapping_changed`` (et non vide) — mêmes
    conditions que le code historique du runner."""

    participants: list = field(default_factory=list)
    updated: int = 0
    created: int = 0
    spk_stats: list = field(default_factory=list)
    propagated: int = 0
    spk_map: dict = field(default_factory=dict)
    spk_map_speakers: list = field(default_factory=list)
    mapping_changed: bool = False


def apply_speaker_roles(
    speaker_roles: dict,
    participants: list,
    mapping_data: dict,
    speaker_stats_data: dict,
) -> SpeakerRolesProjection:
    """Projette les rôles/labels déduits par la LLM sur les structures locuteurs.

    Met à jour ``participants`` (rôles, noms encore vides ou restés SPEAKER_XX),
    propage les labels dans les stats et le mapping. Ne remplace JAMAIS un nom déjà
    validé par l'utilisateur : la LLM ne sert qu'à préremplir. Idempotent.
    """
    mapping = (mapping_data or {}).get("mapping", {})
    if not isinstance(participants, list):
        participants = []

    # Index participants par id et par nom (insensible à la casse)
    by_id = {p["id"]: p for p in participants if p.get("id")}
    by_name = {p["name"].lower(): p for p in participants if p.get("name")}

    updated = 0
    created = 0
    for speaker_id, info in speaker_roles.items():
        normalized = normalize_speaker_role_info(info)
        role = normalized["role"]
        label = normalized["label"]
        if not role:
            continue

        # Trouver le participant via speaker_mapping → participant_id ou nom
        participant = None
        spk_map_entry = mapping.get(speaker_id, {})
        pid = spk_map_entry.get("participant_id", "")
        name = spk_map_entry.get("name", "")

        if pid and pid in by_id:
            participant = by_id[pid]
        elif name and name.lower() in by_name:
            participant = by_name[name.lower()]

        if participant is not None:
            if label and participant.get("name") in ("", speaker_id):
                participant["name"] = label
            if not participant.get("role"):
                participant["role"] = role
                updated += 1
            else:
                current_role = str(participant.get("role", "") or "").strip()
                current_normalized = normalize_speaker_role_info({"label": "", "role": current_role})
                if current_normalized["label"] and current_normalized["role"]:
                    participant["role"] = current_normalized["role"]
                    updated += 1
        else:
            # Créer une entrée minimale si participants.json est vide ou SPEAKER_XX inconnu
            new_p = {
                "id": speaker_id.lower().replace("_", ""),
                "name": label or name or speaker_id,
                "function": "",
                "service": "",
                "role": role,
                "is_animator": False,
                "expected": True,
                "comment": "",
            }
            participants.append(new_p)
            by_id[new_p["id"]] = new_p
            created += 1

    # Propager les noms LLM dans speaker_stats et speaker_mapping même si
    # participants était déjà à jour (appel idempotent). Ne jamais remplacer un
    # nom déjà validé : la LLM ne préremplit que les champs vides ou SPEAKER_XX.
    spk_stats = (speaker_stats_data or {}).get("speakers", [])
    spk_map = (mapping_data or {}).get("mapping", {})
    spk_map_speakers = (mapping_data or {}).get("speakers", [])
    propagated = 0
    mapping_changed = False
    for speaker_id, info in speaker_roles.items():
        norm = normalize_speaker_role_info(info)
        label = norm["label"]
        if not label:
            continue
        for spk in spk_stats:
            if spk.get("speaker_id") == speaker_id:
                current = str(spk.get("mapped_name", "") or "").strip()
                if current in {"", speaker_id}:
                    spk["mapped_name"] = label
                    propagated += 1
        if speaker_id in spk_map:
            current = str(spk_map[speaker_id].get("name", "") or "").strip()
            if current in {"", speaker_id}:
                spk_map[speaker_id]["name"] = label
                mapping_changed = True
        for ms in spk_map_speakers:
            if ms.get("speaker_id") == speaker_id:
                current = str(ms.get("mapped_name", "") or "").strip()
                if current in {"", speaker_id}:
                    ms["mapped_name"] = label
                    mapping_changed = True

    return SpeakerRolesProjection(
        participants=participants,
        updated=updated,
        created=created,
        spk_stats=spk_stats,
        propagated=propagated,
        spk_map=spk_map,
        spk_map_speakers=spk_map_speakers,
        mapping_changed=mapping_changed,
    )


def build_labeled_segments(segments_data: list, speakers_result: dict) -> list[tuple[str, str]]:
    """Pour chaque segment ASR, attribue le texte à un locuteur uniquement si
    un seul SPEAKER_XX a des tours pyannote dans ce segment.

    Dès que deux locuteurs distincts se chevauchent avec le segment, le texte
    contient les deux voix et ne peut pas être attribué sans timestamps mot par
    mot — le segment est ignoré sans alignement mot-à-mot fiable.
    Retourne une liste ordonnée (speaker_id, texte).
    """
    turns_data = speakers_result.get("turns") or []
    if not turns_data or not segments_data:
        return []

    result = []
    for seg in segments_data:
        text = seg.get("text", "").strip()
        if not text:
            continue
        s_start, s_end = seg.get("start", 0.0), seg.get("end", 0.0)
        if s_end <= s_start:
            continue

        # Chevauchement par locuteur
        overlap: dict[str, float] = {}
        for turn in turns_data:
            ov = min(turn["end"], s_end) - max(turn["start"], s_start)
            if ov > 0:
                spk = turn["speaker"]
                overlap[spk] = overlap.get(spk, 0.0) + ov

        if not overlap:
            continue  # aucun tour pyannote — segment ignoré

        # N'attribuer que si UN SEUL locuteur distinct a des tours dans ce segment.
        # Dès que deux locuteurs différents se chevauchent avec le segment ASR,
        # le texte contient les deux voix — impossible de l'attribuer sans timestamps
        # mot par mot fiable.
        unique_speakers = set(overlap.keys())
        if len(unique_speakers) == 1:
            label = next(iter(unique_speakers))
            result.append((label, truncate_at_word(text, 200)))

    return result


def extract_name_hints(labeled_clean: list) -> tuple[dict, list]:
    """
    Retourne deux structures pour aider le LLM à identifier les prénoms :
    - spk_tops : mots en majuscule en milieu de phrase par locuteur (prénoms potentiels)
    - address_hints : (locuteur_A, prénom, locuteur_B) quand A termine son tour
      en appelant B par son prénom (apostrophe directe)
    """
    _SKIP = frozenset({
        "Le", "La", "Les", "Un", "Une", "Des", "Du", "De", "Ce", "Ça", "Ca",
        "Je", "Tu", "Il", "Elle", "On", "Nous", "Vous", "Ils", "Elles", "Y",
        "Et", "Ou", "Mais", "Donc", "Car", "Or", "Si", "Ni",
        "Euh", "Ben", "Bon", "Ah", "Oh", "Non", "Oui", "Ouais", "OK",
        "Alors", "Apres", "Après", "Parce", "Quand", "Comme", "Avec",
        "Pour", "Dans", "Sur", "Par", "Entre", "Vers",
        "Tout", "Tous", "Toute", "Toutes", "Cette", "Ces",
        "Mon", "Ton", "Son", "Ma", "Ta", "Sa", "Notre", "Votre", "Leur", "Leurs",
        "Aussi", "Même", "Encore", "Voilà", "Voila", "Ici", "Là", "Bien", "Très",
        "Cela", "Celui", "Celle", "Ceux", "Celles", "Moi", "Toi", "Lui", "Eux",
    })

    spk_caps: dict = defaultdict(Counter)
    for label, text in labeled_clean:
        words = text.rstrip("…").split()
        for i, word in enumerate(words):
            if i == 0:
                continue
            prev = words[i - 1].rstrip()
            if prev and prev[-1] in ".!?":
                continue
            # Nettoyer ponctuation et caractères non-latins
            bare = re.sub(r"[,\.!?;:«»\"\'()\[\]؀-ۿ一-鿿぀-ヿ]+", "", word).strip()
            if not bare or not bare[0].isupper() or bare in _SKIP or len(bare) < 3:
                continue
            if bare.isupper():  # sigle tout en majuscules — ignorer
                continue
            spk_caps[label][bare] += 1

    address_hints = []
    for i in range(len(labeled_clean) - 1):
        curr_label, curr_text = labeled_clean[i]
        next_label, _ = labeled_clean[i + 1]
        if curr_label == next_label:
            continue
        clean = curr_text.rstrip("…").strip()
        m = re.search(r"\b([A-ZÁÀÂÉÈÊËÎÏÔÙÛÜÇ][a-záàâéèêëîïôùûüç]{2,})[,\s]*$", clean)
        if m:
            name = m.group(1)
            if name not in _SKIP and len(name) >= 3:
                address_hints.append((curr_label, name, next_label))

    spk_tops = {
        spk: [w for w, _ in counter.most_common(8)]
        for spk, counter in spk_caps.items()
        if counter
    }
    return spk_tops, address_hints


def assign_speaker_genders(
    gender_segments: list,
    turns: list,
    min_overlap_s: float = 1.0,
) -> dict:
    """Croise les segments genre horodatés avec les tours pyannote.

    Retourne {speaker_id: {"gender": "male"|"female"|"", "male_s": float, "female_s": float}}.
    Le genre n'est attribué que si le total de chevauchement >= min_overlap_s
    et que l'un des deux sexes domine l'autre.
    """
    if not gender_segments or not turns:
        return {}

    accum: dict = {}
    for turn in turns:
        spk = turn.get("speaker") or turn.get("speaker_id", "")
        t_start = float(turn.get("start", 0.0))
        t_end = float(turn.get("end", 0.0))
        if not spk or t_end <= t_start:
            continue
        if spk not in accum:
            accum[spk] = {"male_s": 0.0, "female_s": 0.0}
        for seg in gender_segments:
            s_start = float(seg.get("start", 0.0))
            s_end = float(seg.get("end", 0.0))
            label = seg.get("label", "")
            overlap = min(t_end, s_end) - max(t_start, s_start)
            if overlap <= 0 or label not in ("male", "female"):
                continue
            accum[spk][f"{label}_s"] += overlap

    result: dict = {}
    for spk, counts in accum.items():
        male_s = counts["male_s"]
        female_s = counts["female_s"]
        total = male_s + female_s
        if total < min_overlap_s:
            gender = ""
        elif male_s > female_s:
            gender = "male"
        elif female_s > male_s:
            gender = "female"
        else:
            gender = ""
        result[spk] = {"gender": gender, "male_s": round(male_s, 2), "female_s": round(female_s, 2)}
    return result


def inject_speaker_genders(speaker_genders: dict, speaker_stats_data: dict) -> tuple[list, int]:
    """Reporte le genre estimé dans les stats locuteurs (sans écraser un choix utilisateur).

    Normalise au passage le format « speakers = liste de strings » (cas sep=1 :
    run_diarization sur vocals.wav → cache miss → format string réécrit par le
    DiarizerService) en le recomposant depuis le champ ``stats``.
    Retourne (spk_stats normalisées, nombre de locuteurs mis à jour) — l'appelant
    persiste ``speaker_stats.json`` si ce nombre est > 0.
    """
    _raw_stats = (speaker_stats_data or {}).get("speakers") or []
    _diar_stats = (speaker_stats_data or {}).get("stats") or {}
    spk_stats = []
    for s in _raw_stats:
        if isinstance(s, str):
            extra = _diar_stats.get(s, {})
            spk_stats.append({
                "speaker_id": s,
                "label": s,
                "speaking_time_seconds": extra.get("speaking_time_seconds", 0),
                "turn_count": extra.get("turn_count", 0),
                "mapped_to": None,
                "mapped_name": None,
                "validation": "pending",
                "gender": "",
            })
        else:
            spk_stats.append(s)
    updated = 0
    for spk in spk_stats:
        spk_id = spk.get("speaker_id", "")
        if spk_id not in speaker_genders:
            continue
        if spk.get("gender"):
            continue  # ne pas écraser un choix utilisateur
        gender = speaker_genders[spk_id]["gender"]
        if gender:
            spk["gender"] = gender
            updated += 1
    return spk_stats, updated


def build_gender_section(audio_scene: dict) -> list:
    """Construit la section genre vocal pour le contexte de diarisation.

    Retourne une liste de lignes Markdown ou ``[]`` si aucune donnée de genre.
    La détection est globale (non attribuée par locuteur) — la section fournit
    un indice supplémentaire au LLM d'identification.
    """
    gender = (audio_scene or {}).get("gender") or {}
    if not gender.get("has_gender_data"):
        return []

    dominant = gender.get("dominant")
    male_ratio = float(gender.get("male_ratio") or 0.0)
    female_ratio = float(gender.get("female_ratio") or 0.0)

    stats_labels = ((audio_scene or {}).get("stats") or {}).get("labels") or {}
    male_dur = float((stats_labels.get("male") or {}).get("duration_s", 0.0))
    female_dur = float((stats_labels.get("female") or {}).get("duration_s", 0.0))

    if dominant == "male":
        dominant_label, dominant_pct = "Masculin", round(male_ratio * 100, 1)
    elif dominant == "female":
        dominant_label, dominant_pct = "Féminin", round(female_ratio * 100, 1)
    else:
        dominant_label, dominant_pct = "Indéterminé", 50.0

    lines = [
        "",
        "## Genre vocal estimé (analyse acoustique globale)",
        "",
        "*(Estimation par fréquence fondamentale — indicatif,"
        " non attribué par locuteur)*",
        "",
        f"- Genre dominant : **{dominant_label}** ({dominant_pct}% de la parole genrée)",
        f"- Parole masculine estimée : {male_dur:.1f}s"
        f" | féminine : {female_dur:.1f}s",
    ]

    if dominant_pct >= 80 and dominant in ("male", "female"):
        adj = "masculine" if dominant == "male" else "féminine"
        lines.append(
            f"- Indice fort : {dominant_pct}% de la parole genrée est {adj}"
        )

    return lines


def render_diarization_context(
    segments_data: list,
    speakers_result: dict,
    audio_scene: dict | None = None,
    speaker_genders: dict | None = None,
) -> str | None:
    """Contenu de ``summary/diarization_context.md`` (ou None si aucun locuteur)."""
    speakers = speakers_result.get("speakers") or []
    if not speakers:
        return None

    labeled = build_labeled_segments(segments_data, speakers_result)

    total_time = sum(float(spk.get("speaking_time_seconds", 0) or 0) for spk in speakers)
    lines = [
        "# Données de diarization acoustique",
        "",
        f"**Nombre de locuteurs détectés :** {len(speakers)}",
        "",
        "| Locuteur | Temps de parole | Tours de parole | Part du temps |",
        "|---|---:|---:|---:|",
    ]
    for spk in sorted(speakers, key=lambda s: float(s.get("speaking_time_seconds", 0) or 0), reverse=True):
        speaking_time = float(spk.get("speaking_time_seconds", 0) or 0)
        turns = int(spk.get("turn_count", 0) or 0)
        pct = round(100 * speaking_time / total_time, 1) if total_time > 0 else 0
        speaker_id = spk.get("speaker_id", spk.get("label", "SPEAKER_XX"))
        lines.append(
            f"| {speaker_id} "
            f"| {speaking_time:.1f}s ({speaking_time / 60:.1f}min) "
            f"| {turns} | {pct}% |"
        )

    # Ne garder que les segments clairement attribués (hors mixte et inconnus)
    labeled_clean = [(lbl, txt) for lbl, txt in labeled if lbl not in ("mixte", "?")]
    if labeled_clean:
        lines.extend([
            "",
            "## Transcription labellisée (attribution acoustique)",
            "",
            "*(uniquement les segments où un seul locuteur parle nettement)*",
            "",
        ])
        for label, text in labeled_clean:
            lines.append(f"**[{label}]** {text}")

        # Résumé des phrases certaines par locuteur (hors mixte)
        by_spk: dict = defaultdict(list)
        for label, text in labeled:
            if label not in ("mixte", "?"):
                by_spk[label].append(f'« {text} »')

        if by_spk:
            lines.extend([
                "",
                "## Ce que dit chaque locuteur (phrases acoustiquement certaines, hors segments mixtes)",
                "",
                "*(Source primaire pour identifier les rôles — ces phrases ont été produites"
                " physiquement par ce SPEAKER_XX)*",
                "",
            ])
            for spk_id in sorted(by_spk.keys()):
                lines.append(f"- **{spk_id}** : {' | '.join(by_spk[spk_id])}")

        # Section indices prénoms
        spk_tops, address_hints = extract_name_hints(labeled_clean)
        if spk_tops or address_hints:
            lines.extend([
                "",
                "## Indices pour identifier les prénoms des locuteurs",
                "",
                "*(Ces données sont des indices bruts — le LLM doit raisonner sur leur pertinence)*",
                "",
            ])
            if address_hints:
                lines.append("### Apostrophes directes détectées (fin de tour → changement de locuteur)")
                lines.append("")
                lines.append("*(Si SPEAKER_A termine son tour en prononçant un prénom et que SPEAKER_B prend la parole,"
                             " SPEAKER_B est probablement ce prénom)*")
                lines.append("")
                seen_hints: set = set()
                for curr_spk, name, next_spk in address_hints:
                    key = (curr_spk, name, next_spk)
                    if key not in seen_hints:
                        lines.append(f"- {curr_spk} dit « …{name} » → {next_spk} prend la parole")
                        seen_hints.add(key)
            if spk_tops:
                lines.extend(["", "### Noms propres en milieu de phrase par locuteur"])
                lines.append("")
                lines.append("*(mots en majuscule hors début de phrase et hors sigles —"
                             " peuvent être des personnes mentionnées ou le prénom du locuteur lui-même)*")
                lines.append("")
                for spk_id in sorted(spk_tops.keys()):
                    names = spk_tops[spk_id]
                    if names:
                        lines.append(f"- **{spk_id}** : {', '.join(names)}")

    # Section genre vocal global (si analyse de scène disponible)
    gender_lines = build_gender_section(audio_scene or {})
    if gender_lines:
        lines.extend(gender_lines)

    # Section genre par locuteur (si attribution acoustique disponible)
    if speaker_genders:
        _GENDER_FR = {"male": "Masculin", "female": "Féminin"}
        _GENDER_SYM = {"male": "♂", "female": "♀"}
        per_spk_lines = [
            "",
            "## Genre vocal par locuteur (estimation acoustique)",
            "",
            "*(Croisement tours pyannote × segments YIN — indicatif)*",
            "",
        ]
        for sid in sorted(speaker_genders.keys()):
            v = speaker_genders[sid]
            gender = v.get("gender", "")
            label = _GENDER_FR.get(gender, "Indéterminé")
            sym = _GENDER_SYM.get(gender, "?")
            female_s = v.get("female_s", 0.0)
            male_s = v.get("male_s", 0.0)
            per_spk_lines.append(
                f"- **{sid}** : {label} {sym}"
                f" ({female_s:.1f}s♀ / {male_s:.1f}s♂)"
            )
        lines.extend(per_spk_lines)

    lines.extend(
        [
            "",
            "**Consigne :** utilise la section 'Ce que dit chaque locuteur' comme données primaires"
            " pour attribuer les SPEAKER_XX à leurs rôles. Déduis le rôle de chaque locuteur depuis"
            " ce qu'il dit dans ses segments certains (qui pose des questions, qui offre, qui commande,"
            " qui réagit, qui encaisse). Ne renverse pas ce mapping : si SPEAKER_XX dit un impératif"
            " ('Goûtez', 'Tenez', 'Regardez') ou annonce un prix, il est l'animateur/hôte/vendeur."
            " Le nombre de locuteurs détectés acoustiquement prime sur les noms mentionnés dans la transcription."
            " Pour les prénoms : utilise en priorité les apostrophes directes ci-dessus"
            " (un locuteur qui appelle la personne suivante par son prénom en fin de tour)."
            " Si un prénom apparaît dans la liste 'Noms propres' d'un locuteur dans un contexte"
            " d'auto-désignation (ex : 'moi, Prénom' ou 'je suis Prénom'), c'est un indice fort.",
            "",
        ]
    )
    return "\n".join(lines)

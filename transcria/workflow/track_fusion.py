"""Fusion des transcriptions PAR PISTE en une timeline globale — module PUR (vague 5, lot B).

Le principe qui rend ce module trivial (cadrage `docs/VAGUE5_PISTES_SEPAREES.md`, D5.1) :
chaque piste est ALIGNÉE sur la timeline commune de la réunion dès la capture — les
timestamps du STT d'une piste SONT ceux de la réunion. La fusion est donc un TRI, pas un
recalage. Les chevauchements deviennent des segments aux intervalles qui se recouvrent,
chacun portant SON locuteur et SES mots — c'est exactement le gain de la vague : dans le
mix, deux voix simultanées étaient une bouillie dont le STT ne sortait qu'un texte.

Aucune I/O ici : les fenêtres viennent du manifeste, les segments des transcripteurs —
la phase orchestre, ce module calcule.
"""
from __future__ import annotations

# Découpe par fenêtres (D5.3) : marge autour de la parole détectée (les attaques/finales
# de mots débordent légèrement des fenêtres d'énergie), et fusion des fenêtres proches
# (relancer le STT toutes les 2 s coûterait plus que transcrire le petit silence).
DEFAULT_WINDOW_MARGIN_S = 0.4
DEFAULT_WINDOW_MERGE_GAP_S = 2.0


def merge_windows(windows, *, margin_s: float = DEFAULT_WINDOW_MARGIN_S,
                  merge_gap_s: float = DEFAULT_WINDOW_MERGE_GAP_S,
                  max_end_s: float | None = None) -> list[tuple[float, float]]:
    """Fenêtres de parole → intervalles à TRANSCRIRE : élargies de `margin_s`, fusionnées
    sous `merge_gap_s` d'écart, bornées à `[0, max_end_s]`. C'est LE levier de coût du
    mode par piste : 2 h de réunion où quelqu'un a parlé 10 min = ~10 min de STT."""
    spans = []
    for raw in windows or ():
        try:
            start, end = float(raw[0]), float(raw[1])
        except (TypeError, ValueError, IndexError):
            continue
        if end <= start:
            continue
        start = max(0.0, start - margin_s)
        end = end + margin_s
        if max_end_s is not None:
            end = min(end, max_end_s)
            if end <= start:
                continue
        spans.append((start, end))
    spans.sort()
    merged: list[list[float]] = []
    for start, end in spans:
        if merged and start - merged[-1][1] <= merge_gap_s:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(round(a, 3), round(b, 3)) for a, b in merged]


def fuse_track_segments(per_track_segments) -> list[dict]:
    """Concatène les segments de toutes les pistes et TRIE par début (départage : fin puis
    locuteur, pour un ordre STABLE). Les timestamps sont globaux par construction — les
    chevauchements inter-pistes sont conservés tels quels : le SRT admet des sous-titres
    aux timecodes qui se recouvrent, et c'est voulu (les mots des DEUX locuteurs existent).
    """
    fused: list[dict] = []
    for segments in per_track_segments:
        fused.extend(segments or ())
    fused.sort(key=lambda s: (float(s.get("start", 0.0)), float(s.get("end", 0.0)),
                              str(s.get("speaker", ""))))
    return fused


def overlapping_indices(segments) -> set[int]:
    """Indices des segments qui CHEVAUCHENT un segment d'un AUTRE locuteur.

    Sert de garde au multi-STT en mode par piste : sur une zone de chevauchement, le mix
    est une bouillie — re-transcrire cette zone DEPUIS LE MIX et arbitrer contre le texte
    de piste reviendrait à défaire le gain de la vague. Ces segments sont exclus de la
    revue (leur texte de piste fait foi). Balayage par ligne (O(n log n))."""
    events: list[tuple[float, int, int]] = []      # (temps, +1/-1, index)
    for i, seg in enumerate(segments):
        try:
            start, end = float(seg.get("start", 0.0)), float(seg.get("end", 0.0))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        events.append((start, 1, i))
        events.append((end, -1, i))
    # À temps égal, les FINS d'abord : deux segments qui se touchent ne se chevauchent pas.
    events.sort(key=lambda e: (e[0], e[1]))
    active: dict[int, str] = {}
    overlapped: set[int] = set()
    for _, kind, i in events:
        if kind == -1:
            active.pop(i, None)
            continue
        speaker = str(segments[i].get("speaker", ""))
        for j, other_speaker in active.items():
            if other_speaker != speaker:
                overlapped.add(i)
                overlapped.add(j)
        active[i] = speaker
    return overlapped


def subtract_intervals(windows, holes) -> list[tuple[float, float]]:
    """Retire `holes` de `windows` (intervalles triés ou non) — sert à écarter la REPISSE
    d'une piste nommée (lot B2, règle de dominance) : les intervalles où pyannote entend
    la voix MINORITAIRE (l'autre participant, capté par le micro) ne sont pas transcrits
    sur CETTE piste — leurs mots vivent sur la piste de leur propriétaire."""
    result: list[tuple[float, float]] = []
    cuts = sorted((float(a), float(b)) for a, b in (holes or ()) if float(b) > float(a))
    for start, end in windows or ():
        start, end = float(start), float(end)
        for cut_start, cut_end in cuts:
            if cut_start >= end:
                break
            if cut_end <= start:
                continue
            if cut_start > start:
                result.append((start, cut_start))
            start = max(start, cut_end)
            if start >= end:
                break
        if end > start:
            result.append((round(start, 3), round(end, 3)))
    return [(round(a, 3), round(b, 3)) for a, b in result]

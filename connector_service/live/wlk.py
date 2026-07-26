"""Adaptateur STT LIVE WhisperLiveKit / Voxtral (L0) — WebSocket JSON `lines` + `buffer`.

Contrairement à ce qu'on avait supposé, WhisperLiveKit n'est PAS du SSE : c'est un WebSocket
qui émet (`audio_processor.py`) un état à deux niveaux —
``{"status":"active_transcription", "lines":[{speaker,text,start,end}…],
   "buffer_transcription":"<queue instable>", "buffer_diarization":"…"}`` — plus
`{"type":"config",…}` au début et `{"type":"ready_to_stop"}` à la fin. Les `lines` sont les
segments CONFIRMÉS (le backend Voxtral committe tout-sauf-le-dernier-mot en interne) ; le
`buffer_transcription` est la queue encore instable.

Mapping : chaque nouvelle `line` = un segment fermé → `committed` + `final` (provenance
`provisional` puis `final_live`) ; `buffer_transcription` → `partial` (affichage gris). Le
CŒUR `WhisperLiveKitParser` est testable ; le WebSocket réel est la glue injectée.
`uses_local_agreement=False` (WLK stabilise déjà en interne).
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable

from connector_service.contract import AudioFrame


def _text_words(text: str, start: float = 0.0, end: float = 0.0) -> list[dict]:
    toks = text.split()
    if not toks:
        return []
    return [{"text": t, "start": start, "end": end} for t in toks]


def _line_words(line: object) -> list[dict]:
    if not isinstance(line, dict):
        return []
    start = float(line.get("start") or 0.0)
    end = float(line.get("end") or 0.0)
    return _text_words(str(line.get("text") or ""), start, end)


class WhisperLiveKitParser:
    """Messages WS WhisperLiveKit → événements normalisés `{committed, partial, final}`.
    Suit le nombre de `lines` déjà émises (WLK renvoie la liste CUMULATIVE) pour n'émettre
    que les nouvelles ; le `buffer_transcription` courant devient la queue `partial`."""

    def __init__(self) -> None:
        self._final_count = 0                   # lignes CLOSES déjà finalisées (final émis)
        self._open_emitted = 0                  # mots de la ligne OUVERTE déjà émis (committed)

    def feed(self, msg: object) -> list[dict]:
        if not isinstance(msg, dict):
            return []
        if msg.get("type") == "ready_to_stop":
            # fin de flux : la dernière ligne ouverte se ferme → final.
            if self._open_emitted > 0:
                self._open_emitted = 0
                self._final_count += 1
                return [{"committed": [], "partial": [], "final": True}]
            return []
        if msg.get("status") != "active_transcription":
            return []                           # config / autre : rien
        raw = msg.get("lines")
        lines = raw if isinstance(raw, list) else []
        out: list[dict] = []
        # Lignes CLOSES = toutes SAUF la dernière (qui peut encore grandir). On finalise
        # celles pas encore émises ; la 1re peut être l'ex-ligne ouverte (émettre son delta).
        for idx in range(self._final_count, max(len(lines) - 1, 0)):
            words = _line_words(lines[idx])
            if idx == self._final_count and self._open_emitted > 0:
                delta = words[self._open_emitted:]
                if delta:
                    out.append({"committed": delta, "partial": [], "final": False})
                out.append({"committed": [], "partial": [], "final": True})
                self._open_emitted = 0
            elif words:
                out.append({"committed": words, "partial": [], "final": True})
        self._final_count = max(self._final_count, max(len(lines) - 1, 0))
        # Ligne OUVERTE (dernière) : n'émettre que le DELTA de mots committés (provisional).
        if lines:
            open_words = _line_words(lines[-1])
            delta = open_words[self._open_emitted:]
            if delta:
                out.append({"committed": delta, "partial": [], "final": False})
                self._open_emitted = len(open_words)
        # Queue encore instable (tampon) → partial.
        buffer = str(msg.get("buffer_transcription") or "").strip()
        if buffer:
            out.append({"committed": [], "partial": _text_words(buffer), "final": False})
        return out


# connect(frames) -> AsyncIterator[dict] : WS WLK ouvert, PCM poussé, messages JSON décodés.
WlkConnect = Callable[[AsyncIterator[AudioFrame]], AsyncIterator[dict]]


def wlk_open_stream(connect: WlkConnect) -> Callable[[AsyncIterator[AudioFrame]],
                                                     AsyncIterator[dict]]:
    """`open_stream` pour `StreamingTranscriber` : déroule le WS WLK (injecté) et normalise
    via `WhisperLiveKitParser`. À passer avec `uses_local_agreement=False`."""
    def _factory(frames: AsyncIterator[AudioFrame]) -> AsyncIterator[dict]:
        async def _open() -> AsyncIterator[dict]:
            parser = WhisperLiveKitParser()
            async for raw in connect(frames):
                for norm in parser.feed(raw):
                    yield norm
        return _open()
    return _factory

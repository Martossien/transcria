"""Client STT LIVE streaming (L0) — `LiveTranscriber` générique.

Le CŒUR (parsing des événements → `Hypothesis`, choix local-agreement vs natif) est
testable en CI ; l'**I/O réel** (connexion SSE audio.cpp Nemotron/Voxtral, ou WebSocket
msgpack Kyutai/moshi) est INJECTÉ via `open_stream` — un adaptateur confirmé contre le
vrai serveur au gate manuel. Un événement du serveur est un dict normalisé :

    {"committed": [{"text","start","end"}, …],   # mots devenus STABLES (delta) → provisional
     "partial":   [{"text","start","end"}, …],   # queue INSTABLE                → partial
     "final": bool}                               # frontière de tour            → final_live

Rétro-compat : un moteur simple peut n'émettre que `{"words": [...]}` (ou `{"text": "..."}`),
traités comme `partial` — un moteur à fenêtre glissante y met son hypothèse cumulative et
`uses_local_agreement=True` laisse la session stabiliser.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

from connector_service.contract import AudioFrame
from connector_service.live.agreement import Word
from connector_service.live.session import Hypothesis

# open_stream(frames) -> AsyncIterator[event_dict] : connecte le serveur STT, pousse
# l'audio, et yield les événements de transcription. Injecté (réel) / factice (CI).
OpenStream = Callable[[AsyncIterator[AudioFrame]], AsyncIterator[dict]]


def _words(raw: object) -> list[Word]:
    if not isinstance(raw, list):
        return []
    return [Word(str(w.get("text") or ""), float(w.get("start") or 0.0),
                 float(w.get("end") or 0.0)) for w in raw if isinstance(w, dict)]


def parse_event(event: dict) -> Hypothesis:
    """Événement serveur normalisé → `Hypothesis` (committed/partial/is_final).

    `committed`/`partial` [{text,start,end}] sont pris tels quels. À défaut, `words`
    (ou `text` découpé) est traité comme la queue `partial` — l'accumulation stable est
    alors laissée au moteur natif (committed) ou au LocalAgreement de la session."""
    committed = _words(event.get("committed"))
    partial = _words(event.get("partial"))
    if not committed and not partial:
        raw_words = event.get("words")
        if isinstance(raw_words, list) and raw_words:
            partial = _words(raw_words)
        else:
            partial = [Word(tok, float(i), float(i + 1))
                       for i, tok in enumerate(str(event.get("text") or "").split())]
    return Hypothesis(committed=committed, partial=partial, is_final=bool(event.get("final")))


class StreamingTranscriber:
    """`LiveTranscriber` : déroule `open_stream` et convertit chaque événement en `Hypothesis`."""

    def __init__(self, open_stream: OpenStream, *, uses_local_agreement: bool = False) -> None:
        self.uses_local_agreement = uses_local_agreement
        self._open = open_stream

    async def stream(self, frames: AsyncIterator[AudioFrame]) -> AsyncIterator[Hypothesis]:
        async for event in self._open(frames):
            yield parse_event(event)


def sse_line_events(read_lines: Callable[[], Awaitable[AsyncIterator[str]]]):
    """Adaptateur SSE (audio.cpp) : transforme un flux de lignes `data: {json}` en
    événements dict. `read_lines` = coroutine ouvrant le flux (réel = HTTP SSE ; injecté).
    Fourni comme brique de branchement — le transport réel est confirmé au gate manuel."""
    import json

    async def _open(frames):
        async for line in await read_lines():
            line = line.strip()
            if line.startswith("data:"):
                payload = line[len("data:"):].strip()
                if payload and payload != "[DONE]":
                    try:
                        yield json.loads(payload)
                    except ValueError:
                        continue
    return _open

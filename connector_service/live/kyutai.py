"""Adaptateur STT LIVE Kyutai / moshi (L0) — WebSocket msgpack `/api/asr-streaming`.

Schéma AUTORITAIRE (source Rust `moshi-server/src/asr.rs`) : audio client→serveur
`{"type":"Audio","pcm":[f32…]}` (24 kHz mono, `use_single_float`), fin `{"type":"Marker"}` ;
serveur→client `Ready` / `Word{text,start_time}` / `EndWord{stop_time}` / `Step{prs,…}` /
`Marker{id}`. Kyutai ne révise JAMAIS un mot — chaque mot est déjà COMMITTÉ ; il n'y a donc
pas de queue instable (`partial` vide) et pas de `final` par mot : la frontière de tour se
déduit d'une pause sémantique (`Step.prs[0] ≥ seuil`) ou du `Marker` de fin de flux.

Ce module fournit le CŒUR testable : `KyutaiAccumulator` qui transforme le flux d'événements
Kyutai (déjà décodés du msgpack) en événements normalisés `{committed, partial, final}`
consommés par `engines.parse_event`. Le WebSocket réel (envoi PCM + décodage msgpack) est la
glue injectée, confirmée au gate manuel. `uses_local_agreement=False` (mots déjà stables).
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable

from connector_service.contract import AudioFrame

ASR_STREAMING_PATH = "/api/asr-streaming"
SAMPLE_RATE_HZ = 24000                 # Kyutai STT : 24 kHz mono float32 (confirmé)
PAUSE_THRESHOLD = 0.25                 # Step.prs[0] (tête 0.5 s) ≥ seuil ⇒ frontière de tour


def audio_message(pcm: list[float]) -> dict:
    """Message audio client→serveur (le transport fait `msgpack.packb(..., use_single_float=True)`).
    ~1 s de SILENCE doit être envoyé AVANT l'audio, et du silence APRÈS + après le Marker
    (délai `asr_delay_in_tokens`) — quirks obligatoires gérés côté transport."""
    return {"type": "Audio", "pcm": pcm}


def marker_message(marker_id: int = 0) -> dict:
    """Marqueur de fin d'audio ; ré-émis par le serveur quand l'audio est drainé."""
    return {"type": "Marker", "id": marker_id}


def _word(text: str, start: float, end: float) -> dict:
    return {"text": text, "start": start, "end": end}


class KyutaiAccumulator:
    """Événements serveur Kyutai → événements normalisés. `feed(event)` renvoie 0..n
    événements `{committed, partial, final}`. Assemble `Word`(text,start) + `EndWord`(stop)
    en un mot committé, et pose la frontière de tour sur pause sémantique / Marker."""

    def __init__(self, pause_threshold: float = PAUSE_THRESHOLD) -> None:
        self._threshold = pause_threshold
        self._pending: tuple[str, float] | None = None      # mot en attente de son EndWord
        self._turn_has_words = False                        # le tour courant a-t-il du contenu ?

    def _flush_pending(self, end: float | None) -> list[dict]:
        if self._pending is None:
            return []
        text, start = self._pending
        self._pending = None
        self._turn_has_words = True
        return [{"committed": [_word(text, start, end if end is not None else start)],
                 "partial": [], "final": False}]

    def _finalize_turn(self) -> list[dict]:
        """Clôt le tour : vide d'abord le mot en attente, puis émet le final SEULEMENT si le
        tour a du contenu (sinon les Steps de pause répétés inonderaient de finals vides)."""
        out = self._flush_pending(None)
        if self._turn_has_words:
            out.append({"committed": [], "partial": [], "final": True})
            self._turn_has_words = False
        return out

    def feed(self, event: object) -> list[dict]:
        if not isinstance(event, dict):
            return []
        etype = event.get("type")
        if etype == "Word":
            # un mot précédent sans EndWord est clos sur son propre start (repli).
            out = self._flush_pending(None)
            self._pending = (str(event.get("text") or ""), float(event.get("start_time") or 0.0))
            return out
        if etype == "EndWord":
            return self._flush_pending(float(event.get("stop_time") or 0.0))
        if etype == "Step":
            prs = event.get("prs") or []
            if prs and float(prs[0]) >= self._threshold:
                return self._finalize_turn()                # pause sémantique = fin de tour
            return []
        if etype == "Marker":
            return self._finalize_turn()                    # fin de flux : clore + finaliser
        return []                                           # Ready / inconnu


# connect(frames) -> AsyncIterator[dict] : WS Kyutai ouvert, PCM poussé, événements décodés.
KyutaiConnect = Callable[[AsyncIterator[AudioFrame]], AsyncIterator[dict]]


def kyutai_open_stream(connect: KyutaiConnect) -> Callable[[AsyncIterator[AudioFrame]],
                                                           AsyncIterator[dict]]:
    """`open_stream` pour `StreamingTranscriber` : déroule le WS Kyutai (injecté) et
    normalise via `KyutaiAccumulator`. À passer à `StreamingTranscriber(..., uses_local_agreement=False)`."""
    def _factory(frames: AsyncIterator[AudioFrame]) -> AsyncIterator[dict]:
        async def _open() -> AsyncIterator[dict]:
            acc = KyutaiAccumulator()
            async for raw in connect(frames):
                for norm in acc.feed(raw):
                    yield norm
        return _open()
    return _factory

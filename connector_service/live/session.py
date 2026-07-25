"""Session LIVE (L0) — orchestration flux audio → segments à provenance progressive.

Consomme les `AudioFrame` d'un `LiveMediaProvider`, les fait passer par une chaîne STT
live, et émet des segments avec provenance :
- `partial` : queue instable (peut changer au prochain paquet), affichage gris ;
- `provisional` : figé en direct (local-agreement OU marqueur natif du moteur) ;
- `final_live` : fin de tour/segment.
Le `canonical` est produit PLUS TARD par le pipeline batch (le direct est un SUIVI).

Provenance définie ICI par valeur (mêmes chaînes que transcria.stt.provenance) — le
connecteur reste ISOLÉ du cœur (contrat import-linter).
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import NamedTuple, Protocol, runtime_checkable

from connector_service.contract import AudioFrame, ExternalMeetingOccurrence
from connector_service.live.agreement import LocalAgreement, Word

PARTIAL = "partial"
PROVISIONAL = "provisional"
FINAL_LIVE = "final_live"


class Hypothesis(NamedTuple):
    words: list[Word]
    is_final: bool = False        # marqueur natif de fin de tour (moteurs streaming)


class Segment(NamedTuple):
    text: str
    start: float
    end: float
    provenance: str


@runtime_checkable
class LiveTranscriber(Protocol):
    """Chaîne STT live. `uses_local_agreement=True` pour un moteur à fenêtres glissantes
    (sans partial/final natifs) ; False pour un moteur au streaming natif (Voxtral SSE)."""

    uses_local_agreement: bool

    def stream(self, frames: AsyncIterator[AudioFrame]) -> AsyncIterator[Hypothesis]: ...


def _segment(words: list[Word], provenance: str) -> Segment:
    return Segment(" ".join(w.text for w in words),
                   words[0].start if words else 0.0,
                   words[-1].end if words else 0.0, provenance)


class LiveSession:
    def __init__(self, transcriber: LiveTranscriber, *,
                 on_partial: Callable[[Segment], None] | None = None,
                 on_provisional: Callable[[Segment], None] | None = None,
                 on_final: Callable[[Segment], None] | None = None) -> None:
        self._t = transcriber
        self._on_partial = on_partial
        self._on_provisional = on_provisional
        self._on_final = on_final

    async def run(self, provider, occurrence: ExternalMeetingOccurrence) -> list[Segment]:
        """Déroule la session. Retourne les segments `final_live` (base du suivi live ;
        le batch produira le `canonical` de référence)."""
        finals: list[Segment] = []
        agree = LocalAgreement() if self._t.uses_local_agreement else None
        async for hyp in self._t.stream(provider.stream_audio(occurrence)):
            if agree is not None:
                newly = agree.insert(hyp.words)
                if newly and self._on_provisional:
                    self._on_provisional(_segment(newly, PROVISIONAL))
                tail = agree.partial(hyp.words)
                if tail and self._on_partial:
                    self._on_partial(_segment(tail, PARTIAL))
                if hyp.is_final:
                    finalized = agree.committed + agree.finalize()
                    seg = _segment(finalized, FINAL_LIVE)
                    finals.append(seg)
                    if self._on_final:
                        self._on_final(seg)
                    agree = LocalAgreement()          # nouveau tour
            else:
                # Moteur au streaming natif : non-final = partial, final = final_live.
                if hyp.is_final:
                    seg = _segment(hyp.words, FINAL_LIVE)
                    finals.append(seg)
                    if self._on_final:
                        self._on_final(seg)
                elif hyp.words and self._on_partial:
                    self._on_partial(_segment(hyp.words, PARTIAL))
        return finals


class LiveConnectorSession:
    """Réunion LIVE de bout en bout (ADR-001 D5) : le SUIVI en direct (segments
    `final_live`) PUIS, à la fin de réunion, l'ingestion de l'enregistrement complet via
    le pont → le pipeline batch produit le `canonical` de référence. Le direct ne remplace
    jamais le batch : il le précède.

    `recording_supplier()` fournit l'audio complet en fin de réunion (artefact post-réunion
    de la plateforme) ; `dedup_key` porte l'idempotence serveur (rejeu → même job).
    """

    def __init__(self, live_session: LiveSession, bridge) -> None:
        self._live = live_session
        self._bridge = bridge

    async def run(self, provider, occurrence, *, recording_supplier, dedup_key: str,
                  filename: str = "recording"):
        finals = await self._live.run(provider, occurrence)          # suivi live
        audio, name = await recording_supplier()                     # enregistrement complet
        result = await self._bridge.ingest_recording(
            audio, name or filename, idempotency_key=dedup_key,
            provider=occurrence.provider, external_meeting_id=occurrence.external_occurrence_id)
        return finals, result

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

from collections.abc import AsyncIterator, Callable, Sequence
from typing import NamedTuple, Protocol, runtime_checkable

from connector_service.contract import AudioFrame, ExternalMeetingOccurrence
from connector_service.live.agreement import LocalAgreement, Word

PARTIAL = "partial"
PROVISIONAL = "provisional"
FINAL_LIVE = "final_live"


class Hypothesis(NamedTuple):
    """État STT à un instant, tel que le livrent les vrais serveurs (audit croisé) :
    aucun n'émet un `final:bool` par mot sur une liste unique — ils séparent le préfixe
    STABLE de la queue INSTABLE. On modélise donc les deux explicitement.

    - `committed` : mots devenus stables à CET événement (delta) → provenance `provisional` ;
      Kyutai (mots jamais révisés) et WhisperLiveKit (`lines` fermées) les remplissent.
    - `partial` : queue encore instable, remplace l'affichage gris → provenance `partial` ;
      pour un moteur à FENÊTRE GLISSANTE (`uses_local_agreement=True`), on y met l'hypothèse
      cumulative COMPLÈTE et le `LocalAgreement` de la session fait la stabilisation.
    - `is_final` : frontière de tour/segment → provenance `final_live`.
    """

    committed: Sequence[Word] = ()
    partial: Sequence[Word] = ()
    is_final: bool = False
    speaker: str = ""             # locuteur (réunion multi-intervenants) — "" si inconnu


class Segment(NamedTuple):
    text: str
    start: float
    end: float
    provenance: str
    speaker: str = ""             # locuteur attribué (vide si la source ne le fournit pas)


@runtime_checkable
class LiveTranscriber(Protocol):
    """Chaîne STT live. `uses_local_agreement=True` pour un moteur à fenêtres glissantes
    (sans partial/final natifs) ; False pour un moteur au streaming natif (Voxtral SSE)."""

    uses_local_agreement: bool

    def stream(self, frames: AsyncIterator[AudioFrame]) -> AsyncIterator[Hypothesis]: ...


def _segment(words: Sequence[Word], provenance: str, speaker: str = "") -> Segment:
    return Segment(" ".join(w.text for w in words),
                   words[0].start if words else 0.0,
                   words[-1].end if words else 0.0, provenance, speaker)


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
        turn: list[Word] = []                          # accumulateur du tour (path natif)
        turn_start = 0                                 # début du tour courant (path LA)
        async for hyp in self._t.stream(provider.stream_audio(occurrence)):
            if agree is not None:
                # Fenêtre glissante : l'hypothèse cumulative complète est dans `partial`.
                full = list(hyp.partial)
                newly = agree.insert(full)
                if newly and self._on_provisional:
                    self._on_provisional(_segment(newly, PROVISIONAL, hyp.speaker))
                tail = agree.partial(full)
                if tail and self._on_partial:
                    self._on_partial(_segment(tail, PARTIAL, hyp.speaker))
                if hyp.is_final:
                    # On NE recrée PAS LocalAgreement : l'hypothèse serveur reste cumulative
                    # (hypothesis[n:] rejouerait tout le début). Un curseur borne le tour.
                    before = agree.committed           # copie AVANT finalize
                    rest = agree.finalize()            # promeut la queue restante
                    turn_words = before[turn_start:] + rest
                    turn_start = len(before) + len(rest)
                    if turn_words:                     # jamais de final vide
                        seg = _segment(turn_words, FINAL_LIVE, hyp.speaker)
                        finals.append(seg)
                        if self._on_final:
                            self._on_final(seg)
            else:
                # Streaming natif : committed = stables (provisional), partial = tail instable.
                if hyp.committed:
                    turn.extend(hyp.committed)
                    if self._on_provisional:
                        self._on_provisional(_segment(hyp.committed, PROVISIONAL, hyp.speaker))
                if hyp.partial and self._on_partial:
                    self._on_partial(_segment(hyp.partial, PARTIAL, hyp.speaker))
                if hyp.is_final:
                    words = turn + list(hyp.partial)   # la queue instable est finalisée
                    if words:                          # jamais de final vide
                        seg = _segment(words, FINAL_LIVE, hyp.speaker)
                        finals.append(seg)
                        if self._on_final:
                            self._on_final(seg)
                    turn = []                          # nouveau tour
        return finals


class LiveConnectorSession:
    """Contrat d'orchestration des CONNECTEURS PLATEFORME (ADR-001 D5) : le SUIVI en
    direct (segments `final_live`) PUIS, à la fin de réunion, la récupération de
    l'ARTEFACT d'enregistrement de la plateforme et son ingestion via le pont → le
    pipeline batch produit le `canonical` de référence. Le direct ne remplace jamais le
    batch : il le précède.

    **Sort tranché (vague 5, D5.6, validé 2026-07-30)** : cette classe est réservée aux
    connecteurs post-réunion des plateformes (Zoom RTMS + Cloud Recording…,
    `TEMPS_REEL_REUNIONS.md` §5) — le BOT n'y passe PAS : il est sa propre source
    d'enregistrement et suit un chemin plus riche (mixage disque, pistes séparées,
    manifeste v2), éprouvé par les gates réels. La câbler dans le bot serait de
    l'abstraction pour l'abstraction.

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

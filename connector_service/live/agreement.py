"""Stabilisation des partiels par LOCAL-AGREEMENT (L0 — ADR-001 D6).

Un mot n'est FIGÉ (`provisional`) que s'il apparaît IDENTIQUE dans DEUX hypothèses STT
consécutives (LocalAgreement-2, d'après `ufal/whisper_streaming`). Réservé aux moteurs à
FENÊTRES GLISSANTES sans marqueurs partial/final natifs — un moteur au streaming natif
(Voxtral SSE) garde sa sémantique et n'utilise PAS ce stabilisateur.

États de provenance produits : le mot instable en tête = `partial` (peut changer) ; dès
qu'il est confirmé = `provisional` (ne bougera plus en direct) ; `final_live`/`canonical`
sont posés en aval.
"""
from __future__ import annotations

from typing import NamedTuple


class Word(NamedTuple):
    text: str
    start: float
    end: float


class LocalAgreement:
    """Alimenté par des hypothèses complètes successives ; confirme le préfixe stable."""

    def __init__(self) -> None:
        self._committed: list[Word] = []
        self._buffer: list[Word] = []      # queue non confirmée de l'hypothèse précédente

    @property
    def committed(self) -> list[Word]:
        """Tous les mots confirmés (`provisional`) jusqu'ici."""
        return list(self._committed)

    def insert(self, hypothesis: list[Word]) -> list[Word]:
        """Ingère l'hypothèse COMPLÈTE courante. Retourne les mots NOUVELLEMENT confirmés
        (préfixe commun avec l'hypothèse précédente, au-delà du déjà-confirmé)."""
        n = len(self._committed)
        tail = hypothesis[n:]              # au-delà du déjà confirmé
        commit: list[Word] = []
        i = 0
        while i < len(tail) and i < len(self._buffer) and tail[i].text == self._buffer[i].text:
            commit.append(tail[i])
            i += 1
        self._committed.extend(commit)
        self._buffer = tail[len(commit):]  # queue de l'hypothèse COURANTE (pour le tour suivant)
        return commit

    def partial(self, hypothesis: list[Word]) -> list[Word]:
        """La queue INSTABLE de l'hypothèse (au-delà du confirmé) = affichage `partial`."""
        return hypothesis[len(self._committed):]

    def finalize(self) -> list[Word]:
        """Fin de tour/segment : promeut la queue restante en confirmé et la retourne."""
        rest = list(self._buffer)
        self._committed.extend(rest)
        self._buffer = []
        return rest

"""Enregistreur de réunion — mixe les pistes captées en UN audio pour le pipeline batch.

Pourquoi c'est indispensable (réunion HYBRIDE) : le direct attribue la parole PAR PISTE, ce
qui marche pour les participants distants (une connexion chacun) mais PAS pour une salle de
réunion, où plusieurs personnes partagent un seul micro — elles arrivent fusionnées sous une
même étiquette. La séparation de ces voix-là relève de la DIARISATION, que le pipeline batch
sait faire (pyannote) et que le direct ne fait pas.

D'où le relais prévu par l'architecture (ADR-001 D5) : le direct produit un suivi immédiat,
puis l'enregistrement complet est ingéré et le batch produit la référence (`canonical`) avec
les locuteurs correctement séparés, salle comprise.

QUALITÉ DU MIXAGE — leçon d'un gate réel : sommer deux voix fortes dépasse l'échelle 16 bits.
Un écrêtage brutal saturait alors l'audio (analyse qualité `suspect`, `degrade_ratio` 1,0) et
ruinait la transcription. On accumule donc en 32 bits puis on NORMALISE l'ensemble d'un facteur
unique : la dynamique relative des locuteurs est préservée, sans distorsion.

Le mixage est PUR et testable : les frames sont placées sur une timeline commune d'après leur
instant d'arrivée, puis sommées avec écrêtage.
"""
from __future__ import annotations

import array
import io
import wave

MAX_S16 = 32767
MIN_S16 = -32768


class MeetingMixer:
    """Mixe des flux PCM `s16le` mono de plusieurs locuteurs sur une timeline commune.

    Chaque frame est écrite à l'offset correspondant à son instant d'arrivée (en secondes
    depuis le début de la réunion) : les silences d'un locuteur pendant que l'autre parle
    sont donc préservés, et la conversation reste intelligible pour la diarisation.
    """

    def __init__(self, sample_rate_hz: int = 48000) -> None:
        self._rate = max(int(sample_rate_hz), 1)
        self._samples = array.array("i")          # accumulateur 32 bits (évite l'écrêtage précoce)

    @property
    def sample_rate_hz(self) -> int:
        return self._rate

    @property
    def duration_s(self) -> float:
        return len(self._samples) / self._rate

    def add(self, pcm: bytes, at_s: float) -> None:
        """Ajoute une frame PCM `s16le` mono à l'instant `at_s` (secondes depuis le début)."""
        if not pcm:
            return
        incoming = array.array("h")
        incoming.frombytes(pcm[: len(pcm) // 2 * 2])
        start = max(int(at_s * self._rate), 0)
        needed = start + len(incoming)
        if needed > len(self._samples):           # étend la timeline avec du silence
            self._samples.extend([0] * (needed - len(self._samples)))
        for i, value in enumerate(incoming):      # somme : plusieurs voix simultanées
            self._samples[start + i] += value

    def to_pcm(self, *, headroom: float = 0.95) -> bytes:
        """Rend le mixage en PCM `s16le`, NORMALISÉ si la somme dépasse l'échelle 16 bits.

        Écrêter la somme saturerait l'audio et ruinerait la transcription (constaté). On
        applique donc un gain unique à tout l'enregistrement : aucune distorsion, et les
        écarts de volume entre locuteurs restent fidèles. `headroom` laisse une marge sous
        le maximum pour éviter tout écrêtage résiduel en aval.
        """
        if not self._samples:
            return b""
        peak = max(abs(v) for v in self._samples)
        limit = int(MAX_S16 * headroom)
        gain = (limit / peak) if peak > limit else 1.0
        out = array.array("h", [max(MIN_S16, min(MAX_S16, int(v * gain)))
                                for v in self._samples])
        return out.tobytes()

    def to_wav(self) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self._rate)
            w.writeframes(self.to_pcm())
        return buf.getvalue()

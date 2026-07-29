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


class ParticipantLedger:
    """Registre PUR des fenêtres de parole par participant — produit le manifeste (vague 2).

    Alimenté au fil des frames captées (mêmes instants `at_s` que le `MeetingMixer` : les
    fenêtres vivent sur la MÊME timeline que le mixage, condition de validité de la projection
    aval). Les frames consécutives d'un même participant sont FUSIONNÉES en fenêtres tant que
    le silence qui les sépare reste sous `merge_gap_s` — sans fusion, un manifeste compterait
    des milliers de micro-fenêtres de 20 ms sans valeur pour la projection.
    """

    def __init__(self, *, merge_gap_s: float = 0.75, min_peak: int = 300) -> None:
        self._gap = max(float(merge_gap_s), 0.0)
        # Seuil d'ÉNERGIE (crête s16) : constat du gate Jitsi réel (2026-07-29) — un bot
        # navigateur reçoit des trames EN CONTINU (bruit de confort), donc « une trame est
        # arrivée » ne veut pas dire « il a parlé » : sans seuil, les fenêtres couvrent toute
        # la réunion et, à plusieurs participants, la marge de projection tuerait toutes les
        # suggestions de noms. Même seuil que la preuve de voix de capture.js (300).
        self._min_peak = int(min_peak)
        self._windows: dict[str, list[list[float]]] = {}
        self._names: dict[str, str] = {}
        self._kinds: dict[str, str] = {}

    def note(self, participant_id: str, name: str, at_s: float, duration_s: float,
             *, kind: str = "unknown", pcm: bytes | None = None) -> None:
        pid = str(participant_id or "").strip()
        if not pid or duration_s <= 0:
            return
        if pcm is not None and self._peak(pcm) < self._min_peak:
            return                                   # trame silencieuse : pas une fenêtre de parole
        if name:
            self._names[pid] = name                  # le dernier nom connu gagne
        if kind in ("solo", "room"):                 # `unknown` n'écrase jamais un vrai type
            self._kinds[pid] = kind
        start, end = max(float(at_s), 0.0), max(float(at_s), 0.0) + float(duration_s)
        windows = self._windows.setdefault(pid, [])
        if windows and start - windows[-1][1] <= self._gap and start >= windows[-1][0]:
            windows[-1][1] = max(windows[-1][1], end)
        else:
            windows.append([start, end])

    @staticmethod
    def _peak(pcm: bytes) -> int:
        samples = array.array("h")
        samples.frombytes(pcm[: len(pcm) // 2 * 2])
        return max((abs(v) for v in samples), default=0)

    def to_manifest(self, source: str) -> dict | None:
        """Le manifeste (contrat §6.3 du plan UI_REUNIONS), ou None si rien n'a été capté —
        un manifeste vide serait REJETÉ par la validation stricte du serveur, autant ne pas
        l'envoyer."""
        if not self._windows:
            return None
        participants = []
        for pid in sorted(self._windows):
            windows = [[round(a, 3), round(b, 3)] for a, b in self._windows[pid]]
            participants.append({
                "id": pid,
                "name": self._names.get(pid, ""),
                "kind": self._kinds.get(pid, "unknown"),
                "speech_windows": windows,
                "speech_total_s": round(sum(b - a for a, b in self._windows[pid]), 3),
            })
        return {"version": 1, "source": source, "mix": "timeline_common",
                "participants": participants}


class RecordingTee:
    """Transcripteur-enregistreur : délègue le LIVE au moteur interne ET alimente mixage +
    registre — promu du gate Jitsi au BOT DE PRODUCTION (le chaînon manquant du parcours
    100 % interface : capté par le bot docker, l'audio doit devenir un job tout seul).

    Les participants sont déclarés `unknown` (traités en salle par prudence — « piste ≠
    personne ») : le bot ne SAIT pas qu'une connexion est une personne seule.
    """

    def __init__(self, inner, *, sample_rate_hz: int = 48000):
        self._inner = inner
        self.mixer = MeetingMixer(sample_rate_hz)
        self.ledger = ParticipantLedger()
        self._t0: float | None = None
        self.uses_local_agreement = getattr(inner, "uses_local_agreement", False)

    def stream(self, frames):
        import time

        async def _tee():
            async for frame in frames:
                if self._t0 is None:
                    self._t0 = time.monotonic()
                at_s = time.monotonic() - self._t0
                self.mixer.add(frame.payload, at_s)
                rate = getattr(frame, "sample_rate_hz", 48000) or 48000
                self.ledger.note(
                    frame.participant_id,
                    getattr(frame, "participant_display_name", "") or "",
                    at_s, len(frame.payload) / 2.0 / rate,
                    pcm=frame.payload)
                yield frame
        return self._inner.stream(_tee())

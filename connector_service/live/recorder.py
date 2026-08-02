"""Enregistreur de réunion — mix + PISTES SÉPARÉES, sur disque, pour le pipeline batch.

Vague 5, lot A (`docs/archive/VAGUE5_PISTES_SEPAREES.md`) : le direct attribue la parole PAR PISTE ;
le mélange perdait les MOTS des chevauchements (deux voix sommées = bouillie pour le STT) et
condamnait une piste « salle » à rester UN locuteur. Désormais chaque participant a SON
fichier, aligné sur la timeline commune — le mix reste produit (repli, préflight qualité,
compatibilité totale avec les bots anciens et les connecteurs post-réunion sans pistes).

QUALITÉ DU MIXAGE — leçon d'un gate réel : sommer deux voix fortes dépasse l'échelle 16
bits ; on accumule en 32 bits et on NORMALISE d'un gain global (`MixFileWriter`). PLACEMENT
— leçon du 2026-07-30 : continuité d'échantillons par flux (`ContinuityPlacer`), calculée
UNE fois par le mixeur et RÉUTILISÉE pour la piste du même flux : mix et pistes alignés par
construction. MÉMOIRE : tout est sur disque, RAM constante quelle que soit la durée.
"""
from __future__ import annotations

import array
import logging
import re
import tempfile
from pathlib import Path

from connector_service.live.timeline import MixFileWriter, TrackFileWriter

logger = logging.getLogger(__name__)

# Borne de santé locale au bot : au-delà, les pistes excédentaires ne sont PAS écrites
# (le mix les couvre) et le manifeste l'annonce (`track_overflow`) — dégradation dite,
# jamais silencieuse. Le serveur a sa propre garde (config), celle-ci évite juste de
# remplir le disque du conteneur.
DEFAULT_MAX_TRACKS = 16


class MeetingMixer:
    """Mixe des flux PCM `s16le` mono sur la timeline commune — façade DISQUE.

    Conserve le contrat historique (`add(...) -> instant placé`, `duration_s`,
    `to_wav() -> bytes`) au-dessus de `MixFileWriter` ; expose en plus `add_frame`
    (instant + échantillon de départ) pour que l'enregistreur écrive la piste du même
    flux À LA MÊME position.
    """

    def __init__(self, sample_rate_hz: int = 48000, *, resync_gap_s: float = 0.5,
                 path: str | Path | None = None) -> None:
        if path is None:
            path = Path(tempfile.mkdtemp(prefix="transcria_mix_")) / "mix.raw"
        self._writer = MixFileWriter(path, sample_rate_hz, resync_gap_s=resync_gap_s)

    @property
    def sample_rate_hz(self) -> int:
        return self._writer.sample_rate_hz

    @property
    def duration_s(self) -> float:
        return self._writer.duration_s

    def add_frame(self, pcm: bytes, at_s: float, *, stream_id: str = "") -> tuple[float, int]:
        """(instant placé en s, échantillon de départ) — cf. `MixFileWriter.add`."""
        return self._writer.add(pcm, at_s, stream_id=stream_id)

    def add(self, pcm: bytes, at_s: float, *, stream_id: str = "") -> float:
        """Contrat historique : rend l'instant RÉELLEMENT placé (secondes)."""
        placed_s, _ = self.add_frame(pcm, at_s, stream_id=stream_id)
        return placed_s

    def to_wav_file(self, path: str | Path | None = None) -> Path:
        """WAV normalisé sur disque — LE chemin à privilégier (rien en RAM)."""
        return self._writer.finalize_wav(path)

    def to_wav(self) -> bytes:
        """Compat : WAV en mémoire (tests, petits extraits). Pour un enregistrement
        complet, préférer `to_wav_file` + envoi en flux."""
        return self.to_wav_file().read_bytes()


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

    def participant_ids(self) -> set[str]:
        """Participants ayant AU MOINS une fenêtre de parole — les seuls qui figureront au
        manifeste (une piste sans entrée de manifeste serait une part ORPHELINE, rejetée
        en bloc par le serveur)."""
        return set(self._windows)

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


def _track_ref(pid: str, taken: set[str]) -> str:
    """Nom de part multipart d'une piste : `track_<id assaini>` — déterministe, sûr pour
    un nom de fichier, unique (suffixe en cas de collision d'assainissement)."""
    base = re.sub(r"[^A-Za-z0-9_-]", "_", pid)[:56] or "piste"
    ref = f"track_{base}"
    n = 2
    while ref in taken:
        ref = f"track_{base}-{n}"
        n += 1
    taken.add(ref)
    return ref


class RecordingTee:
    """Transcripteur-enregistreur : délègue le LIVE au moteur interne ET alimente mixage +
    pistes + registre — le chaînon du parcours 100 % interface (capté par le bot docker,
    l'audio devient un job tout seul).

    Les participants sont déclarés `unknown` (traités en salle par prudence — « piste ≠
    personne ») : le bot ne SAIT pas qu'une connexion est une personne seule.
    """

    def __init__(self, inner, *, sample_rate_hz: int = 48000,
                 workdir: str | Path | None = None,
                 max_tracks: int = DEFAULT_MAX_TRACKS):
        self._inner = inner
        self._dir = Path(workdir or tempfile.mkdtemp(prefix="transcria_bot_rec_"))
        self._dir.mkdir(parents=True, exist_ok=True)
        self.mixer = MeetingMixer(sample_rate_hz, path=self._dir / "mix.raw")
        self.ledger = ParticipantLedger()
        self._rate = sample_rate_hz
        self._max_tracks = max(int(max_tracks), 0)
        self._tracks: dict[str, TrackFileWriter] = {}
        self._refs: dict[str, str] = {}           # pid → nom de part `track_…`
        self._taken_refs: set[str] = set()
        self.track_overflow = False               # pistes non écrites (au-delà du plafond)
        self.tracks_degraded = False              # écriture piste tombée (disque…) — mix intact
        self._t0: float | None = None
        self.uses_local_agreement = getattr(inner, "uses_local_agreement", False)

    def _track_for(self, pid: str) -> TrackFileWriter | None:
        if self.tracks_degraded or not pid:
            return None
        track = self._tracks.get(pid)
        if track is not None:
            return track
        if len(self._tracks) >= self._max_tracks:
            if not self.track_overflow:
                logger.warning("plafond de %d pistes atteint — les suivantes restent "
                               "couvertes par le MIX (track_overflow annoncé)", self._max_tracks)
            self.track_overflow = True
            return None
        ref = _track_ref(pid, self._taken_refs)
        track = TrackFileWriter(self._dir / f"{ref}.raw", self._rate)
        self._tracks[pid] = track
        self._refs[pid] = ref
        return track

    def stream(self, frames):
        import time

        async def _tee():
            async for frame in frames:
                if self._t0 is None:
                    self._t0 = time.monotonic()
                at_s = time.monotonic() - self._t0
                pid = str(frame.participant_id or "")
                # Le mixeur DÉCIDE du placement (continuité par flux) ; la piste du même
                # flux écrit À LA MÊME position — alignement par construction.
                placed_s, start_sample = self.mixer.add_frame(frame.payload, at_s,
                                                              stream_id=pid)
                track = self._track_for(pid)
                if track is not None:
                    try:
                        track.write_at(start_sample, frame.payload)
                    except OSError:
                        # Disque plein / erreur d'E/S : le MIX (prioritaire) continue, la
                        # réunion n'est JAMAIS perdue — dégradation annoncée au manifeste.
                        logger.exception("écriture de piste impossible — bascule en mode "
                                         "mix seul (tracks_degraded)")
                        self.tracks_degraded = True
                rate = getattr(frame, "sample_rate_hz", 48000) or 48000
                self.ledger.note(
                    pid,
                    getattr(frame, "participant_display_name", "") or "",
                    placed_s, len(frame.payload) / 2.0 / rate,
                    pcm=frame.payload)
                yield frame
        return self._inner.stream(_tee())

    def _shippable_pids(self) -> set[str]:
        """Pistes à EMBARQUER : non vides ET présentes au manifeste (fenêtres de parole).
        Un micro resté coupé produit une piste de bruit de confort sans la moindre fenêtre :
        l'envoyer ferait une part ORPHELINE que le serveur rejetterait en bloc (règle
        tout-ou-rien de D5.2) — cohérence garantie ICI, à la source."""
        return {pid for pid, track in self._tracks.items()
                if track.duration_s > 0 and pid in self.ledger.participant_ids()}

    def track_files(self) -> dict[str, Path]:
        """{nom de part → WAV finalisé} des pistes réellement UTILES (cf. `_shippable_pids`)."""
        return {self._refs[pid]: self._tracks[pid].finalize()
                for pid in sorted(self._shippable_pids())}

    def to_manifest(self, source: str) -> dict | None:
        """Manifeste v2 : celui du registre, enrichi des références de pistes. Sans piste
        finalisée → v1 à l'identique (compatibilité totale, règle D5.2)."""
        manifest = self.ledger.to_manifest(source)
        if manifest is None:
            return None
        refs = {pid: self._refs[pid] for pid in self._shippable_pids()}
        if not refs or self.tracks_degraded:
            if self.tracks_degraded:
                manifest["tracks_degraded"] = True
            return manifest
        for entry in manifest["participants"]:
            ref = refs.get(entry["id"])
            if ref:
                entry["track"] = ref
        manifest["version"] = 2
        if self.track_overflow:
            manifest["track_overflow"] = True
        return manifest

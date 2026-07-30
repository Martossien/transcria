"""Enregistreur de réunion : mixage multi-locuteurs sur une timeline commune."""
from __future__ import annotations

import array
import io
import struct
import wave

from connector_service.live.recorder import MeetingMixer

RATE = 16000


def _tone(value: int, samples: int) -> bytes:
    return struct.pack(f"<{samples}h", *([value] * samples))


def _read(wav: bytes) -> array.array:
    with wave.open(io.BytesIO(wav)) as w:
        data = array.array("h")
        data.frombytes(w.readframes(w.getnframes()))
        return data


def test_place_les_frames_a_leur_instant():
    """Une frame arrivée à 1 s doit être précédée d'une seconde de silence."""
    mix = MeetingMixer(RATE)
    mix.add(_tone(1000, RATE // 10), at_s=1.0)
    samples = _read(mix.to_wav())
    assert len(samples) == RATE + RATE // 10
    assert set(samples[:RATE]) == {0}                    # silence de tête préservé
    assert samples[RATE] == 1000


def test_somme_les_locuteurs_simultanes():
    """Deux voix au même instant se SUPERPOSENT (c'est un enregistrement de réunion)."""
    mix = MeetingMixer(RATE)
    mix.add(_tone(1000, 100), at_s=0.0, stream_id="a")
    mix.add(_tone(500, 100), at_s=0.0, stream_id="b")
    samples = _read(mix.to_wav())
    assert samples[0] == 1500                            # 1000 + 500


def test_normalise_au_lieu_d_ecreter():
    """Régression : l'écrêtage saturait l'audio et ruinait la transcription. La somme qui
    dépasse l'échelle doit être NORMALISÉE (gain global), pas tronquée."""
    mix = MeetingMixer(RATE)
    for stream in range(4):
        # 4 locuteurs simultanés : somme = 120000, très au-delà de 32767
        mix.add(_tone(30000, 10), at_s=0.0, stream_id=f"s{stream}")
    samples = _read(mix.to_wav())
    assert samples[0] <= 32767                           # dans l'échelle
    assert samples[0] >= 30000                           # …et proche du maximum (pas atténué à l'excès)


def test_normalisation_preserve_les_rapports_de_volume():
    """Le gain est GLOBAL : un locuteur deux fois plus fort le reste après normalisation."""
    mix = MeetingMixer(RATE)
    mix.add(_tone(30000, 10), at_s=0.0, stream_id="a")   # fort
    mix.add(_tone(30000, 10), at_s=0.0, stream_id="b")   # → 60000 cumulés, à normaliser
    mix.add(_tone(15000, 10), at_s=1.0, stream_id="c")   # moitié moins fort
    samples = _read(mix.to_wav())
    fort, faible = samples[0], samples[RATE]
    assert fort <= 32767
    assert abs(fort / faible - 4.0) < 0.05               # rapport 60000/15000 conservé


def test_pas_de_normalisation_si_inutile():
    mix = MeetingMixer(RATE)
    mix.add(_tone(1000, 10), at_s=0.0)
    assert _read(mix.to_wav())[0] == 1000                # signal faible : inchangé


def test_wav_valide_et_duree():
    mix = MeetingMixer(RATE)
    mix.add(_tone(800, RATE), at_s=0.0)                  # 1 seconde
    with wave.open(io.BytesIO(mix.to_wav())) as w:
        assert w.getframerate() == RATE and w.getnchannels() == 1 and w.getsampwidth() == 2
        assert w.getnframes() == RATE
    assert abs(mix.duration_s - 1.0) < 1e-6


def test_frame_vide_ignoree():
    mix = MeetingMixer(RATE)
    mix.add(b"", at_s=0.0)
    assert mix.duration_s == 0.0


class TestContinuiteDeFlux:
    """Leçon du gate Jitsi du 2026-07-30 : placées à leur instant d'ARRIVÉE, les frames d'une
    même voix se chevauchaient ou se trouaient au gré de la gigue réseau — filtrage en peigne
    mesuré (bande 99 % effondrée à 2,5 kHz, STOI 0,43). La continuité d'échantillons doit
    primer sur l'horloge d'arrivée à l'intérieur d'un même flux."""

    def test_la_gigue_ne_deplace_pas_l_audio(self):
        """Frames de 10 ms arrivées en retard/avance (< resync) : placées BOUT À BOUT."""
        mix = MeetingMixer(RATE)
        n = RATE // 100                                   # 10 ms
        assert mix.add(_tone(1000, n), at_s=0.0, stream_id="a") == 0.0
        # arrivée en RETARD de 8 ms : sans continuité, 8 ms de silence s'intercaleraient
        placed = mix.add(_tone(1000, n), at_s=0.018, stream_id="a")
        assert placed == n / RATE                         # placée en continuité, pas à 0.018
        # arrivée en AVANCE (rafale) : sans continuité, elle ÉCRASERAIT la précédente
        placed = mix.add(_tone(1000, n), at_s=0.019, stream_id="a")
        assert placed == 2 * n / RATE
        samples = _read(mix.to_wav())
        assert len(samples) == 3 * n                      # aucun trou, aucun chevauchement
        assert set(samples) == {1000}

    def test_resynchronise_apres_une_vraie_coupure(self):
        """Micro coupé puis rendu : au-delà de `resync_gap_s`, l'horloge d'arrivée reprend
        la main — le silence de la coupure est PRÉSERVÉ dans l'enregistrement."""
        mix = MeetingMixer(RATE, resync_gap_s=0.5)
        n = RATE // 100
        mix.add(_tone(1000, n), at_s=0.0, stream_id="a")
        placed = mix.add(_tone(1000, n), at_s=2.0, stream_id="a")
        assert placed == 2.0                              # ré-ancrée à son arrivée
        samples = _read(mix.to_wav())
        assert set(samples[n:2 * RATE]) == {0}            # la coupure reste du silence

    def test_les_flux_sont_independants(self):
        """La continuité vaut PAR flux : elle ne colle pas les locuteurs entre eux."""
        mix = MeetingMixer(RATE)
        n = RATE // 100
        mix.add(_tone(1000, n), at_s=0.0, stream_id="a")
        placed = mix.add(_tone(500, n), at_s=0.02, stream_id="b")
        assert placed == 0.02                             # « b » s'ancre à SA première arrivée


class TestLedgerEnergyThreshold:
    """Constat du gate Jitsi réel : les trames coulent en continu (bruit de confort) — sans
    seuil d'énergie, les fenêtres de parole couvrent toute la réunion et la projection perd
    tout pouvoir de suggestion à plusieurs participants."""

    def test_trame_silencieuse_ignoree_trame_sonore_comptee(self):
        import array

        from connector_service.live.recorder import ParticipantLedger
        led = ParticipantLedger()
        silence = array.array("h", [10] * 480).tobytes()      # crête 10 << 300
        voice = array.array("h", [5000] * 480).tobytes()
        led.note("p1", "Alice", 0.0, 0.5, pcm=silence)
        assert led.to_manifest("jitsi") is None               # rien capté de PARLÉ
        led.note("p1", "Alice", 1.0, 0.5, pcm=voice)
        m = led.to_manifest("jitsi")
        assert m["participants"][0]["speech_windows"] == [[1.0, 1.5]]

    def test_sans_pcm_comportement_historique(self):
        from connector_service.live.recorder import ParticipantLedger
        led = ParticipantLedger()
        led.note("p1", "Alice", 0.0, 0.5)                     # pas de PCM = pas de jugement
        assert led.to_manifest("jitsi") is not None

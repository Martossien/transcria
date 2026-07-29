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
    mix.add(_tone(1000, 100), at_s=0.0)
    mix.add(_tone(500, 100), at_s=0.0)
    samples = _read(mix.to_wav())
    assert samples[0] == 1500                            # 1000 + 500


def test_normalise_au_lieu_d_ecreter():
    """Régression : l'écrêtage saturait l'audio et ruinait la transcription. La somme qui
    dépasse l'échelle doit être NORMALISÉE (gain global), pas tronquée."""
    mix = MeetingMixer(RATE)
    for _ in range(4):
        mix.add(_tone(30000, 10), at_s=0.0)              # somme = 120000, très au-delà de 32767
    samples = _read(mix.to_wav())
    assert samples[0] <= 32767                           # dans l'échelle
    assert samples[0] >= 30000                           # …et proche du maximum (pas atténué à l'excès)


def test_normalisation_preserve_les_rapports_de_volume():
    """Le gain est GLOBAL : un locuteur deux fois plus fort le reste après normalisation."""
    mix = MeetingMixer(RATE)
    mix.add(_tone(30000, 10), at_s=0.0)                  # fort
    mix.add(_tone(30000, 10), at_s=0.0)                  # → 60000 cumulés, à normaliser
    mix.add(_tone(15000, 10), at_s=1.0)                  # moitié moins fort
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

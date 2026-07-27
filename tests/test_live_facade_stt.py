"""STT live par fenêtres : découpage par locuteur + parole simultanée (cœur testable)."""
from __future__ import annotations

import asyncio
import struct
import wave

from connector_service.contract import AudioFrame, ExternalMeetingOccurrence
from connector_service.live.facade_stt import (
    FacadeTranscriber,
    SpeakerBuffer,
    frame_peak,
    pcm_to_wav,
)
from connector_service.live.session import LiveSession

RATE = 16000
FRAME_MS = 20
SAMPLES = RATE * FRAME_MS // 1000          # 320 échantillons = 20 ms

OCC = ExternalMeetingOccurrence(provider="bot", provider_account_id="a",
                                external_occurrence_id="reunion")


def _pcm(peak: int) -> bytes:
    return struct.pack(f"<{SAMPLES}h", *([peak] * SAMPLES))


VOICE = _pcm(8000)                          # frame « parlée »
SILENCE = _pcm(0)                           # frame silencieuse


def _frame(speaker: str, payload: bytes, name: str = "") -> AudioFrame:
    return AudioFrame(provider="bot", provider_account_id="a", external_occurrence_id="reunion",
                      track_id=f"t-{speaker}", sequence_number=0, media_timestamp_ms=0,
                      wall_clock_timestamp="2026-07-26T00:00:00Z", duration_ms=FRAME_MS,
                      encoding="pcm_s16le", sample_rate_hz=RATE, channels=1,
                      sample_count=SAMPLES, payload=payload,
                      participant_id=speaker, participant_display_name=name or None)


def test_frame_peak_et_wav():
    assert frame_peak(VOICE) == 8000 and frame_peak(SILENCE) == 0
    assert frame_peak(b"") == 0
    wav = pcm_to_wav(VOICE, RATE)
    with wave.open(__import__("io").BytesIO(wav)) as w:      # en-tête WAV valide
        assert w.getframerate() == RATE and w.getnchannels() == 1 and w.getsampwidth() == 2


def test_buffer_ferme_le_tour_sur_une_pause():
    buf = SpeakerBuffer(RATE, min_window_s=0.1, max_window_s=10, silence_to_close_s=0.1,
                        min_voiced_s=0.02)
    assert buf.add(VOICE) is None                    # trop court encore
    for _ in range(4):
        buf.add(VOICE)
    window = None
    for _ in range(10):                              # la pause finit par clore le tour
        window = buf.add(SILENCE)
        if window:
            break
    assert window is not None and len(window) > 0


def test_buffer_borne_la_duree_maximale():
    buf = SpeakerBuffer(RATE, min_window_s=0.1, max_window_s=0.1, silence_to_close_s=99,
                        min_voiced_s=0.02)
    got = [buf.add(VOICE) for _ in range(6)]
    assert any(w is not None for w in got)           # fermé par la durée max, sans pause


def test_buffer_jette_les_fenetres_muettes():
    buf = SpeakerBuffer(RATE, min_window_s=0.02, max_window_s=0.04, silence_to_close_s=0.02,
                        min_voiced_s=0.02)
    for _ in range(6):
        buf.add(SILENCE)
    assert buf.flush() is None                       # que du silence → rien à transcrire


class _Provider:
    def __init__(self, frames):
        self._frames = frames

    async def stream_audio(self, _occurrence):
        for f in self._frames:
            yield f
            await asyncio.sleep(0)


def _run(frames, transcribe):
    transcriber = FacadeTranscriber(transcribe, min_window_s=0.05, max_window_s=0.1, min_voiced_s=0.02,
                                    silence_to_close_s=0.04)
    session = LiveSession(transcriber)
    return asyncio.run(session.run(_Provider(frames), OCC))


def test_deux_locuteurs_en_alternance_sont_attribues():
    """Alternance : chacun doit recevoir SON texte, avec son nom."""
    frames = ([_frame("alice", VOICE, "Alice")] * 6 + [_frame("alice", SILENCE, "Alice")] * 3
              + [_frame("bob", VOICE, "Bob")] * 6 + [_frame("bob", SILENCE, "Bob")] * 3)
    textes = {"alice": "bonjour à tous", "bob": "merci Alice"}
    seen = []

    def transcribe(wav: bytes) -> str:
        seen.append(len(wav))
        return textes["alice"] if len(seen) == 1 else textes["bob"]

    finals = _run(frames, transcribe)
    speakers = {s.speaker for s in finals}
    assert speakers == {"Alice", "Bob"}                       # les DEUX locuteurs ressortent
    assert any("bonjour" in s.text for s in finals)
    assert all(s.provenance == "final_live" for s in finals)


def test_parole_SIMULTANEE_reste_separee():
    """Deux personnes qui parlent EN MÊME TEMPS : tampons indépendants → 2 transcriptions
    distinctes (l'atout de la capture par piste face à un flux mixé)."""
    frames = []
    for _ in range(6):                                        # frames entrelacées
        frames.append(_frame("alice", VOICE, "Alice"))
        frames.append(_frame("bob", VOICE, "Bob"))
    for _ in range(3):
        frames.append(_frame("alice", SILENCE, "Alice"))
        frames.append(_frame("bob", SILENCE, "Bob"))

    def transcribe(_wav: bytes) -> str:
        return "texte"

    finals = _run(frames, transcribe)
    assert {s.speaker for s in finals} == {"Alice", "Bob"}    # aucun mélange
    assert len(finals) >= 2


def test_une_fenetre_en_echec_n_arrete_pas_la_reunion():
    frames = [_frame("alice", VOICE, "Alice")] * 6 + [_frame("alice", SILENCE, "Alice")] * 3
    calls = {"n": 0}

    def transcribe(_wav: bytes) -> str:
        calls["n"] += 1
        raise RuntimeError("moteur STT indisponible")

    finals = _run(frames, transcribe)                         # ne lève pas
    assert finals == [] and calls["n"] >= 1


# --------------------------------------------------------------------------- #
#  Quantité minimale de parole — protection contre les hallucinations du moteur
# --------------------------------------------------------------------------- #
# Ces tests verrouillent une règle établie par MESURE sur l'installation réelle : un moteur
# de type Whisper à qui l'on soumet du silence n'échoue pas, il INVENTE. Douze secondes de
# silence numérique pur ont produit une phrase française complète et entièrement fausse, et
# des segments de ce genre polluaient les transcriptions de réunion Zoom.

def test_fenetre_sans_aucune_parole_est_jetee():
    buf = SpeakerBuffer(RATE, min_window_s=0.02, max_window_s=0.1, silence_to_close_s=99)
    fenetres = [buf.add(SILENCE) for _ in range(20)]
    assert all(f is None for f in fenetres), "du silence ne doit JAMAIS partir au moteur"


def test_fenetre_avec_trop_peu_de_parole_est_jetee():
    """Le cas qui polluait les réunions : un claquement de clavier ou une respiration
    ouvrait une fenêtre presque vide, aussitôt transformée en texte imaginaire."""
    buf = SpeakerBuffer(RATE, min_window_s=0.02, max_window_s=0.5,
                        silence_to_close_s=99, min_voiced_s=0.35)
    buf.add(VOICE)                                   # 20 ms de son : très en deçà du seuil
    fenetres = [buf.add(SILENCE) for _ in range(40)]  # jusqu'au plafond de durée
    assert all(f is None for f in fenetres)


def test_fenetre_avec_assez_de_parole_est_transmise():
    buf = SpeakerBuffer(RATE, min_window_s=0.02, max_window_s=5.0,
                        silence_to_close_s=0.1, min_voiced_s=0.1)
    for _ in range(10):                              # 200 ms de parole : au-dessus du seuil
        buf.add(VOICE)
    fenetre = None
    for _ in range(10):
        fenetre = buf.add(SILENCE)
        if fenetre:
            break
    assert fenetre is not None and len(fenetre) > 0


def test_la_parole_se_cumule_sur_des_bribes_separees():
    """Une interjection hachée (« oui… d'accord ») doit compter comme de la parole : le seuil
    porte sur le CUMUL, pas sur une salve continue."""
    buf = SpeakerBuffer(RATE, min_window_s=0.02, max_window_s=5.0,
                        silence_to_close_s=99, min_voiced_s=0.1)
    for _ in range(6):                               # 6 × (20 ms parlés + 20 ms silence)
        buf.add(VOICE)
        buf.add(SILENCE)
    assert buf.voiced_s >= 0.1
    assert buf.flush() is not None


def test_flush_final_respecte_le_seuil():
    """Le vidage de fin de réunion ne doit pas contourner la règle : c'est justement là qu'un
    reliquat de silence traînait."""
    buf = SpeakerBuffer(RATE, min_voiced_s=0.35)
    for _ in range(50):
        buf.add(SILENCE)
    assert buf.flush() is None


def test_le_compteur_de_parole_se_remet_a_zero_apres_une_fenetre():
    """Sans remise à zéro, la parole d'une fenêtre validerait les suivantes — et le silence
    qui suit un vrai tour repartirait au moteur."""
    buf = SpeakerBuffer(RATE, min_window_s=0.02, max_window_s=5.0,
                        silence_to_close_s=99, min_voiced_s=0.1)
    for _ in range(10):
        buf.add(VOICE)
    assert buf.flush() is not None
    assert buf.voiced_s == 0.0
    for _ in range(50):
        buf.add(SILENCE)
    assert buf.flush() is None


def test_le_seuil_de_production_ecarte_une_frame_isolee():
    """Verrou sur le DÉFAUT, pas sur une valeur de test : c'est lui qui protège en réunion."""
    from connector_service.live.facade_stt import MIN_VOICED_S

    buf = SpeakerBuffer(RATE)                        # aucun réglage : valeurs de production
    buf.add(VOICE)
    assert buf.voiced_s < MIN_VOICED_S
    assert buf.flush() is None

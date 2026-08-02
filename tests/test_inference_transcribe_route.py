"""Route `/infer/transcribe` du nœud de calcul : deux transports, options, erreurs.

Le moteur est INJECTÉ : aucun modèle n'est chargé, ces tests tournent sans GPU.
"""
from __future__ import annotations

import io
import wave

import pytest

from inference_service.app import create_app
from inference_helpers import inference_dev_config


class _FakeEngine:
    """Moteur STT factice : enregistre les appels et rend des segments contrôlés."""

    def __init__(self, segments=None, boom: Exception | None = None):
        self.segments = segments if segments is not None else [
            {"start": 0.0, "end": 1.5, "text": "bonjour"},
            {"start": 1.5, "end": 2.0, "text": "à tous"},
        ]
        self.boom = boom
        self.calls: list[dict] = []

    def transcribe(self, audio_path, *, language="fr", backend=None):
        self.calls.append({"path": str(audio_path), "language": language, "backend": backend})
        if self.boom:
            raise self.boom
        return self.segments


def _client(engine=None, config=None):
    app = create_app(config=inference_dev_config(config), transcribe_engine=engine or _FakeEngine())
    return app.test_client()


def _wav_bytes(seconds: float = 0.1, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


def test_upload_transcrit_et_agrege_le_texte():
    engine = _FakeEngine()
    resp = _client(engine).post("/infer/transcribe", data={
        "file": (io.BytesIO(_wav_bytes()), "extrait.wav"), "language": "fr"},
        content_type="multipart/form-data")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["text"] == "bonjour à tous"          # texte agrégé prêt à l'emploi
    assert len(body["segments"]) == 2
    assert body["segments"][0] == {"start": 0.0, "end": 1.5, "text": "bonjour"}
    assert engine.calls[0]["language"] == "fr"


def test_reference_fichier(tmp_path):
    audio = tmp_path / "reunion.wav"
    audio.write_bytes(_wav_bytes())
    engine = _FakeEngine()
    resp = _client(engine, config={"inference": {"allowed_audio_roots": [str(tmp_path)]}}).post(
        "/infer/transcribe", json={"audio_path": str(audio), "language": "en"})
    assert resp.status_code == 200
    assert engine.calls[0]["language"] == "en"
    assert engine.calls[0]["path"] == str(audio)


def test_moteur_choisi_par_l_appelant():
    engine = _FakeEngine()
    _client(engine).post("/infer/transcribe", data={
        "file": (io.BytesIO(_wav_bytes()), "a.wav"), "backend": "cohere"},
        content_type="multipart/form-data")
    assert engine.calls[0]["backend"] == "cohere"


def test_langue_par_defaut_quand_absente():
    """Le nœud ne doit pas transmettre `None` au moteur : défaut explicite."""
    engine = _FakeEngine()
    _client(engine).post("/infer/transcribe", data={
        "file": (io.BytesIO(_wav_bytes()), "a.wav")}, content_type="multipart/form-data")
    assert engine.calls[0]["language"] == "fr"


def test_fichier_manquant_est_un_400():
    resp = _client().post("/infer/transcribe", data={}, content_type="multipart/form-data")
    assert resp.status_code == 400


def test_extension_non_supportee_refusee():
    resp = _client().post("/infer/transcribe", data={
        "file": (io.BytesIO(b"pas de l'audio"), "document.pdf")},
        content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "unsupported_format" in (resp.get_data(as_text=True) or "")


def test_chemin_sans_audio_path_est_un_400():
    assert _client().post("/infer/transcribe", json={}).status_code == 400


def test_fichier_inexistant_est_un_400(tmp_path):
    resp = _client(config={"inference": {"allowed_audio_roots": [str(tmp_path)]}}).post(
        "/infer/transcribe", json={"audio_path": str(tmp_path / "absent.wav")})
    assert resp.status_code == 400


def test_segments_vides_donnent_un_texte_vide():
    resp = _client(_FakeEngine(segments=[])).post("/infer/transcribe", data={
        "file": (io.BytesIO(_wav_bytes()), "a.wav")}, content_type="multipart/form-data")
    assert resp.status_code == 200 and resp.get_json()["text"] == ""


@pytest.mark.parametrize("segments", [
    [{"start": None, "end": None, "text": None}],       # champs nuls
    ["pas un dict"],                                    # entrée aberrante
])
def test_segments_aberrants_ne_font_pas_planter(segments):
    """Le nœud normalise : un moteur exotique ne doit pas produire un 500."""
    resp = _client(_FakeEngine(segments=segments)).post("/infer/transcribe", data={
        "file": (io.BytesIO(_wav_bytes()), "a.wav")}, content_type="multipart/form-data")
    assert resp.status_code == 200

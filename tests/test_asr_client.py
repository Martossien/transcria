"""Tests du client ASR OpenAI (vLLM) — parsing, auth, retry, distinction d'erreurs.

Aucun serveur réel : une fausse session HTTP capture les requêtes et renvoie des
réponses scriptées.
"""
from __future__ import annotations

import wave
from pathlib import Path

import pytest

from transcria.inference.asr_client import AsrClient, build_asr_client_from_config
from transcria.inference.client import InferenceRequestError, InferenceUnavailable


class _Resp:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _Session:
    """Session factice : enregistre les appels et rejoue une file de réponses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, data=None, files=None, headers=None, timeout=None):
        self.calls.append({"url": url, "data": data, "files": files, "headers": headers})
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": headers})
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


@pytest.fixture
def wav(tmp_path) -> Path:
    p = tmp_path / "a.wav"
    with wave.open(str(p), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * 1600)
    return p


def test_transcribe_verbose_json(wav):
    sess = _Session([_Resp(200, {"text": "bonjour", "segments": [{"start": 0, "end": 1, "text": "bonjour"}]})])
    client = AsrClient("http://h:8001/v1", model="cohere-transcribe", session=sess)
    out = client.transcribe(wav, language="fr")
    assert out["segments"][0]["text"] == "bonjour"
    call = sess.calls[0]
    assert call["url"] == "http://h:8001/v1/audio/transcriptions"
    assert call["data"]["model"] == "cohere-transcribe"
    assert call["data"]["language"] == "fr"
    assert "file" in call["files"]


def test_auth_header_when_key(wav):
    sess = _Session([_Resp(200, {"text": "x"})])
    client = AsrClient("http://h/v1", model="m", api_key="secret", session=sess)
    client.transcribe(wav)
    assert sess.calls[0]["headers"]["Authorization"] == "Bearer secret"


def test_no_auth_header_without_key(wav):
    sess = _Session([_Resp(200, {"text": "x"})])
    client = AsrClient("http://h/v1", model="m", session=sess)
    client.transcribe(wav)
    assert "Authorization" not in sess.calls[0]["headers"]


def test_4xx_is_request_error(wav):
    sess = _Session([_Resp(400, {"error": {"message": "bad audio", "code": "invalid_file"}})])
    client = AsrClient("http://h/v1", model="m", retries=0, session=sess)
    with pytest.raises(InferenceRequestError) as ei:
        client.transcribe(wav)
    assert ei.value.status == 400
    assert ei.value.code == "invalid_file"


def test_503_is_unavailable_and_retries(wav):
    sess = _Session([_Resp(503, {"error": {"message": "loading"}}), _Resp(200, {"text": "ok"})])
    client = AsrClient("http://h/v1", model="m", retries=1, session=sess)
    out = client.transcribe(wav)
    assert out["text"] == "ok"
    assert len(sess.calls) == 2  # une 503 puis succès


def test_connection_error_is_unavailable(wav):
    import requests

    sess = _Session([requests.exceptions.ConnectionError("refused")])
    client = AsrClient("http://h/v1", model="m", retries=0, session=sess)
    with pytest.raises(InferenceUnavailable):
        client.transcribe(wav)


def test_health_checks_models(wav):
    sess = _Session([_Resp(200, {"data": [{"id": "cohere-transcribe"}]})])
    client = AsrClient("http://h/v1", model="cohere-transcribe", session=sess)
    assert client.health() is True
    assert sess.calls[0]["url"] == "http://h/v1/models"


def test_build_from_config_none_when_no_url():
    assert build_asr_client_from_config({"inference": {"stt": {"backends": {}}}}, "cohere") is None


def test_build_from_config_reads_backend(monkeypatch):
    monkeypatch.setenv("MYKEY", "k123")
    cfg = {
        "inference": {
            "stt": {
                "auth": {"api_key_env": "MYKEY"},
                "response_format": "json",
                "backends": {"whisper": {"url": "http://h:8005/v1", "model": "whisper-large-v3"}},
            }
        }
    }
    client = build_asr_client_from_config(cfg, "whisper")
    assert client is not None
    assert client.base_url == "http://h:8005/v1"
    assert client.model == "whisper-large-v3"
    assert client.api_key == "k123"
    assert client.response_format == "json"


def test_response_format_per_backend_overrides_global():
    """Cohere ne supporte pas verbose_json : l'override par backend prime sur le global."""
    cfg = {
        "inference": {
            "stt": {
                "response_format": "verbose_json",  # défaut global
                "backends": {
                    "cohere": {"url": "http://h:8003/v1", "model": "cohere-transcribe",
                               "response_format": "json"},          # override
                    "whisper": {"url": "http://h:8005/v1", "model": "whisper-large-v3"},
                },
            }
        }
    }
    assert build_asr_client_from_config(cfg, "cohere").response_format == "json"
    assert build_asr_client_from_config(cfg, "whisper").response_format == "verbose_json"

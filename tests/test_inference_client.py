"""Tests du client d'inférence frontend + RemoteDiarizer + factory.

Aucun réseau réel : la session HTTP et le client sont mockés. On valide le
contrat, la distinction indisponible/4xx, le retry, l'auth, les transports,
le fallback local et le routing de la factory.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from transcria.inference.client import (
    FailoverInferenceClient,
    InferenceClient,
    InferenceRequestError,
    InferenceUnavailable,
    build_client_from_config,
)

# ── Fausse session HTTP ────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, status_code, json_body=None, text=""):
        self.status_code = status_code
        self._json = json_body
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class _FakeSession:
    """Enregistre les appels et rejoue des réponses scriptées."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, "kwargs": kwargs})
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp

    def get(self, url, **kwargs):
        self.calls.append({"url": url, "kwargs": kwargs, "method": "get"})
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


_DIAR_OK = {"available": True, "turns": [{"start": 0, "end": 1, "speaker": "S0", "duration": 1}],
            "exclusive_turns": [], "speakers": ["S0"], "stats": {}}


# ── Client : transports & contrat ──────────────────────────────────────────────

def test_diarize_file_ref_envoie_json():
    sess = _FakeSession([_FakeResponse(200, _DIAR_OK)])
    client = InferenceClient("http://svc:8002", transport="file_ref", session=sess, retries=0)
    out = client.diarize(Path("/x/a.wav"))
    assert out == _DIAR_OK
    call = sess.calls[0]
    assert call["url"] == "http://svc:8002/infer/diarize"
    assert call["kwargs"]["json"] == {"audio_path": "/x/a.wav"}


def test_voice_embed_endpoint():
    sess = _FakeSession([_FakeResponse(200, {"dim": 8})])
    client = InferenceClient("http://svc:8002", session=sess, retries=0)
    out = client.voice_embed(Path("/x/a.wav"))
    assert out == {"dim": 8}
    assert sess.calls[0]["url"].endswith("/infer/voice-embed")


def test_auth_bearer_ajoute(tmp_path):
    sess = _FakeSession([_FakeResponse(200, _DIAR_OK)])
    client = InferenceClient("http://svc", api_key="k-42", session=sess, retries=0)
    client.diarize(Path("/x/a.wav"))
    assert sess.calls[0]["kwargs"]["headers"]["Authorization"] == "Bearer k-42"


def test_sans_cle_pas_de_header():
    sess = _FakeSession([_FakeResponse(200, _DIAR_OK)])
    client = InferenceClient("http://svc", session=sess, retries=0)
    client.diarize(Path("/x/a.wav"))
    assert "Authorization" not in sess.calls[0]["kwargs"]["headers"]


def test_upload_envoie_multipart(tmp_path):
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF....")
    sess = _FakeSession([_FakeResponse(200, _DIAR_OK)])
    client = InferenceClient("http://svc", transport="upload", session=sess, retries=0)
    client.diarize(audio)
    assert "files" in sess.calls[0]["kwargs"]


# ── Distinction indisponible vs 4xx ────────────────────────────────────────────

def test_503_est_indisponible():
    sess = _FakeSession([_FakeResponse(503, {"error": "gpu_busy", "message": "VRAM"})])
    client = InferenceClient("http://svc", session=sess, retries=0)
    with pytest.raises(InferenceUnavailable):
        client.diarize(Path("/x/a.wav"))


def test_4xx_est_request_error():
    sess = _FakeSession([_FakeResponse(403, {"error": "path_not_allowed", "message": "nope"})])
    client = InferenceClient("http://svc", session=sess, retries=0)
    with pytest.raises(InferenceRequestError) as ei:
        client.diarize(Path("/x/a.wav"))
    assert ei.value.status == 403
    assert ei.value.code == "path_not_allowed"


def test_exception_reseau_est_indisponible():
    import requests
    sess = _FakeSession([requests.exceptions.ConnectionError("refused")])
    client = InferenceClient("http://svc", session=sess, retries=0)
    with pytest.raises(InferenceUnavailable):
        client.diarize(Path("/x/a.wav"))


# ── Retry ──────────────────────────────────────────────────────────────────────

def test_retry_puis_succes(monkeypatch):
    monkeypatch.setattr("transcria.inference.client.time.sleep", lambda _s: None)
    sess = _FakeSession([_FakeResponse(503, {"error": "gpu_busy"}), _FakeResponse(200, _DIAR_OK)])
    client = InferenceClient("http://svc", session=sess, retries=1)
    assert client.diarize(Path("/x/a.wav")) == _DIAR_OK
    assert len(sess.calls) == 2


def test_retry_epuise_leve_indisponible(monkeypatch):
    monkeypatch.setattr("transcria.inference.client.time.sleep", lambda _s: None)
    sess = _FakeSession([_FakeResponse(503, {}), _FakeResponse(503, {})])
    client = InferenceClient("http://svc", session=sess, retries=1)
    with pytest.raises(InferenceUnavailable):
        client.diarize(Path("/x/a.wav"))


def test_health_true_false():
    assert InferenceClient("http://svc", session=_FakeSession([_FakeResponse(200)])).health() is True
    import requests
    assert InferenceClient(
        "http://svc", session=_FakeSession([requests.exceptions.ConnectionError("x")])
    ).health() is False


# ── build_client_from_config ────────────────────────────────────────────────--

def test_build_client_none_si_pas_durl():
    assert build_client_from_config({}) is None
    assert build_client_from_config({"inference": {}}) is None


def test_build_client_lit_url_et_transport():
    cfg = {"inference": {"url": "http://gpu:8002/", "transport": {"audio": "upload"},
                         "resilience": {"timeout_s": 60, "retries": 3}}}
    client = build_client_from_config(cfg)
    assert client is not None
    assert client.base_url == "http://gpu:8002"
    assert client.transport == "upload"
    assert client.timeout_s == 60
    assert client.retries == 3


def test_build_client_cle_depuis_env(monkeypatch):
    monkeypatch.setenv("MY_INF_KEY", "secret-xyz")
    cfg = {"inference": {"url": "http://gpu:8002", "auth": {"api_key_env": "MY_INF_KEY"}}}
    client = build_client_from_config(cfg)
    assert client.api_key == "secret-xyz"


# ── Failover actif/passif (C6 / B7) ─────────────────────────────────────────--

def test_build_client_single_node_reste_simple():
    """Un seul nœud (via `url`) → client simple, pas de bascule (compat)."""
    client = build_client_from_config({"inference": {"url": "http://gpu:8002"}})
    assert isinstance(client, InferenceClient)
    assert not isinstance(client, FailoverInferenceClient)


def test_build_client_nodes_ordonne_par_priorite():
    cfg = {"inference": {"nodes": [
        {"url": "http://secours:8002", "priority": 2},
        {"url": "http://principal:8002", "priority": 1},
    ]}}
    client = build_client_from_config(cfg)
    assert isinstance(client, FailoverInferenceClient)
    assert client.nodes == ["http://principal:8002", "http://secours:8002"]
    assert client.base_url == "http://principal:8002"   # primaire = priorité la plus basse


def test_build_client_nodes_unique_reste_simple():
    """`nodes` à un seul élément → pas de wrapper de bascule inutile."""
    cfg = {"inference": {"nodes": [{"url": "http://gpu:8002", "priority": 1}]}}
    client = build_client_from_config(cfg)
    assert isinstance(client, InferenceClient)
    assert not isinstance(client, FailoverInferenceClient)
    assert client.base_url == "http://gpu:8002"


def test_build_client_nodes_dedup_et_fallback_url():
    # Doublons retirés en préservant l'ordre.
    c1 = build_client_from_config({"inference": {"nodes": [
        {"url": "http://a:8002", "priority": 1},
        {"url": "http://a:8002", "priority": 2},
        {"url": "http://b:8002", "priority": 3},
    ]}})
    assert isinstance(c1, FailoverInferenceClient)
    assert c1.nodes == ["http://a:8002", "http://b:8002"]
    # `nodes` vide → on retombe sur `url`.
    c2 = build_client_from_config({"inference": {"nodes": [], "url": "http://solo:8002"}})
    assert isinstance(c2, InferenceClient) and not isinstance(c2, FailoverInferenceClient)


def test_build_client_nodes_partage_auth_et_transport(monkeypatch):
    monkeypatch.setenv("INF_KEY", "k-9")
    cfg = {"inference": {
        "nodes": [{"url": "http://a:8002"}, {"url": "http://b:8002"}],
        "auth": {"api_key_env": "INF_KEY"},
        "transport": {"audio": "upload"},
        "resilience": {"timeout_s": 42, "retries": 0},
    }}
    client = build_client_from_config(cfg)
    assert isinstance(client, FailoverInferenceClient)
    for node in client._clients:
        assert node.api_key == "k-9"
        assert node.transport == "upload"
        assert node.timeout_s == 42


def _node(base_url, responses):
    """InferenceClient réel adossé à une session scriptée (retries=0, pas de sleep)."""
    return InferenceClient(base_url, session=_FakeSession(responses), retries=0)


def _conn_err():
    import requests
    return requests.exceptions.ConnectionError("refused")


def test_failover_bascule_sur_indisponible():
    """Principal injoignable → bascule transparente vers le secours."""
    primary = _node("http://principal:8002", [_conn_err()])
    backup = _node("http://secours:8002", [_FakeResponse(200, _DIAR_OK)])
    client = FailoverInferenceClient([primary, backup])
    assert client.diarize(Path("/x/a.wav")) == _DIAR_OK
    assert len(primary._session.calls) == 1 and len(backup._session.calls) == 1


def test_failover_prefere_le_primaire_quand_il_repond():
    """Les deux nœuds sont up → on n'appelle QUE le primaire (préférence priorité)."""
    primary = _node("http://principal:8002", [_FakeResponse(200, {"who": "primary"})])
    backup = _node("http://secours:8002", [_FakeResponse(200, {"who": "backup"})])
    client = FailoverInferenceClient([primary, backup])
    assert client.diarize(Path("/x/a.wav")) == {"who": "primary"}
    assert len(backup._session.calls) == 0


def test_failover_4xx_ne_bascule_pas():
    """Erreur métier 4xx → pas de bascule (l'entrée est en cause partout)."""
    primary = _node("http://principal:8002", [_FakeResponse(422, {"error": "bad"})])
    backup = _node("http://secours:8002", [_FakeResponse(200, _DIAR_OK)])
    client = FailoverInferenceClient([primary, backup])
    with pytest.raises(InferenceRequestError):
        client.diarize(Path("/x/a.wav"))
    assert len(backup._session.calls) == 0


def test_failover_tous_indisponibles_leve_indisponible():
    primary = _node("http://principal:8002", [_conn_err()])
    backup = _node("http://secours:8002", [_conn_err()])
    client = FailoverInferenceClient([primary, backup])
    with pytest.raises(InferenceUnavailable):
        client.diarize(Path("/x/a.wav"))
    assert len(primary._session.calls) == 1 and len(backup._session.calls) == 1


def test_failover_capabilities_bascule():
    primary = _node("http://principal:8002", [_conn_err()])
    backup = _node("http://secours:8002", [_FakeResponse(200, {"mode": "remote"})])
    client = FailoverInferenceClient([primary, backup])
    assert client.capabilities() == {"mode": "remote"}


def test_failover_health_vrai_si_un_noeud_repond():
    down = _node("http://principal:8002", [_conn_err()])
    up = _node("http://secours:8002", [_FakeResponse(200)])
    assert FailoverInferenceClient([down, up]).health() is True
    only_down = _node("http://principal:8002", [_conn_err()])
    assert FailoverInferenceClient([only_down]).health() is False


def test_failover_exige_au_moins_un_noeud():
    with pytest.raises(ValueError):
        FailoverInferenceClient([])


# ── RemoteDiarizer ──────────────────────────────────────────────────────────--

class _FakeClient:
    def __init__(self, result=None, raise_exc=None):
        self.result = result
        self.raise_exc = raise_exc
        self.calls = 0

    def diarize(self, audio_path):
        self.calls += 1
        if self.raise_exc:
            raise self.raise_exc
        return self.result


def _remote(config, client):
    from transcria.stt.remote_diarizer import RemoteDiarizer
    return RemoteDiarizer(config, device="cpu", client=client)


def _job():
    from transcria.jobs.models import Job, JobState
    return Job(id="remote-diar-job", owner_id="u1", title="T", state=JobState.CREATED.value)


def test_remote_diarizer_persiste_le_resultat(tmp_path):
    from transcria.jobs.filesystem import JobFilesystem
    cfg = {"storage": {"jobs_dir": str(tmp_path)}, "diarization": {"cache_enabled": False,
           "embedding_cache_enabled": False}, "inference": {"diarization": {"fallback_local": False}}}
    client = _FakeClient(result=_DIAR_OK)
    diar = _remote(cfg, client)
    out = diar.diarize(_job(), tmp_path / "a.wav")
    assert out["available"] is True
    fs = JobFilesystem(str(tmp_path), "remote-diar-job")
    assert fs.load_json("speakers/speaker_turns.json")["speakers"] == ["S0"]


def test_remote_diarizer_fallback_local(tmp_path, monkeypatch):
    cfg = {"storage": {"jobs_dir": str(tmp_path)}, "diarization": {"cache_enabled": False},
           "inference": {"diarization": {"fallback_local": True}}}
    client = _FakeClient(raise_exc=InferenceUnavailable("service down"))

    # Le fallback construit un DiarizerService local : on le mocke pour éviter pyannote.
    called = {}

    class _FakeLocal:
        def __init__(self, *a, **k):
            pass

        def diarize(self, job, audio_path):
            called["yes"] = True
            return {"available": True, "turns": [], "speakers": ["LOCAL"], "stats": {}}

    monkeypatch.setattr("transcria.stt.diarization.DiarizerService", _FakeLocal)
    diar = _remote(cfg, client)
    out = diar.diarize(_job(), tmp_path / "a.wav")
    assert called.get("yes") is True
    assert out["speakers"] == ["LOCAL"]


def test_remote_diarizer_sans_fallback_renvoie_erreur(tmp_path):
    cfg = {"storage": {"jobs_dir": str(tmp_path)}, "diarization": {"cache_enabled": False},
           "inference": {"diarization": {"fallback_local": False}}}
    client = _FakeClient(raise_exc=InferenceUnavailable("down"))
    diar = _remote(cfg, client)
    out = diar.diarize(_job(), tmp_path / "a.wav")
    assert out["available"] is False
    assert "service_indisponible" in out["error"]


def test_remote_diarizer_model_name_distinct():
    diar = _remote({"models": {"pyannote_model": "pyannote/x"}}, _FakeClient(result=_DIAR_OK))
    assert diar.model_name == "remote:pyannote/x"  # ne collisionne pas avec le cache local


def test_remote_diarizer_available():
    assert _remote({}, _FakeClient(result=_DIAR_OK)).available is True
    assert _remote({}, None).available is False


# ── Factory ─────────────────────────────────────────────────────────────────--

def test_factory_route_vers_remote():
    from transcria.stt.diarizer_factory import create_diarizer, list_available_backends
    from transcria.stt.remote_diarizer import RemoteDiarizer
    cfg = {"models": {"diarization_backend": "remote"}, "inference": {"url": "http://svc:8002"}}
    diar = create_diarizer(cfg, device="cpu")
    assert isinstance(diar, RemoteDiarizer)
    assert "remote" in list_available_backends()

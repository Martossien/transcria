"""Tests du statut des ressources distantes et de la politique d'admission (étape 5).

Pur, sans réseau. Couvre remote_requirements, assess_admission (admit/queue/fail),
summarize_capabilities, et InferenceClient.capabilities() avec une session factice.
"""
from __future__ import annotations

import pytest

from transcria.inference.client import InferenceClient, InferenceUnavailable
from transcria.inference.resource_status import (
    assess_admission,
    available_remote_slots,
    remote_requirements,
    remote_vram_admits,
    summarize_capabilities,
)

# ── remote_requirements ───────────────────────────────────────────────────────

def test_requirements_empty_when_all_local():
    assert remote_requirements({}) == set()
    assert remote_requirements({"inference": {"mode": "local"}}) == set()


def test_requirements_detects_each_capability():
    cfg = {
        "models": {"stt_backend": "cohere", "diarization_backend": "remote"},
        "inference": {
            "mode": "remote",
            "url": "http://h:8002",
            "stt": {"backends": {"cohere": {"url": "http://h:8003/v1"}}},
        },
    }
    assert remote_requirements(cfg) == {"stt", "diarize", "voice_embed"}


def test_requirements_partial_hybrid():
    # STT distant uniquement (diarize local, pas d'url service → pas de voice_embed).
    cfg = {
        "models": {"stt_backend": "whisper", "diarization_backend": "pyannote"},
        "inference": {"mode": "hybrid", "stt": {"backends": {"whisper": {"url": "http://h:8005/v1"}}}},
    }
    assert remote_requirements(cfg) == {"stt"}


# ── assess_admission ──────────────────────────────────────────────────────────

_REMOTE_CFG = {
    "models": {"stt_backend": "cohere"},
    "inference": {"mode": "remote", "stt": {"backends": {"cohere": {"url": "http://h/v1"}}},
                  "resilience": {"max_unavailable_s": 300}},
}


def test_admit_when_all_local():
    v = assess_admission({}, reachable=False, unavailable_for_s=99999)
    assert v.action == "admit"   # rien de distant → toujours admis


def test_admit_when_remote_reachable():
    assert assess_admission(_REMOTE_CFG, reachable=True).action == "admit"


def test_queue_when_unreachable_within_window():
    v = assess_admission(_REMOTE_CFG, reachable=False, unavailable_for_s=120)
    assert v.action == "queue"


def test_fail_when_unreachable_beyond_window():
    v = assess_admission(_REMOTE_CFG, reachable=False, unavailable_for_s=301)
    assert v.action == "fail"
    assert "300" in v.reason


def test_window_default_600s():
    cfg = {
        "models": {"stt_backend": "cohere"},
        "inference": {"mode": "remote", "stt": {"backends": {"cohere": {"url": "http://h/v1"}}}},
    }
    assert assess_admission(cfg, reachable=False, unavailable_for_s=599).action == "queue"
    assert assess_admission(cfg, reachable=False, unavailable_for_s=600).action == "fail"


# ── summarize_capabilities ────────────────────────────────────────────────────

def test_summarize_unreachable():
    s = summarize_capabilities(None)
    assert s == {"reachable": False, "mode": None, "gpus": [], "engines": []}


def test_summarize_reachable():
    caps = {
        "deployment_mode": "resource_node",
        "gpus": [{"index": 3, "free_mb": 20000, "total_mb": 24000}],
        "inprocess": [{"name": "voice-embed", "loaded": False, "capacity": 1, "inflight": 0, "queued": 2, "busy": False}],
        "stt_engines": [
            {"name": "cohere", "up": True, "ensure_in_progress": True},
            {"name": "whisper", "up": False},
        ],
    }
    s = summarize_capabilities(caps)
    assert s["reachable"] is True
    assert s["mode"] == "resource_node"
    assert s["gpus"][0]["index"] == 3
    by = {e["name"]: e for e in s["engines"]}
    assert by["cohere"] == {"name": "cohere", "kind": "stt", "up": True, "ensure_in_progress": True}
    assert by["whisper"]["up"] is False
    assert by["voice-embed"] == {
        "name": "voice-embed",
        "kind": "inprocess",
        "up": True,
        "loaded": False,
        "capacity": 1,
        "inflight": 0,
        "queued": 2,
        "busy": False,
    }


# ── available_remote_slots ───────────────────────────────────────────────────

def test_available_remote_slots_empty_for_local_or_missing_payload():
    assert available_remote_slots({}, {"stt_engines": []}) is None
    assert available_remote_slots(_REMOTE_CFG, None) is None


def test_available_remote_slots_uses_stt_concurrency_when_engine_up():
    cfg = {
        "models": {"stt_backend": "cohere"},
        "inference": {
            "mode": "remote",
            "stt": {
                "concurrency": 4,
                "backends": {"cohere": {"url": "http://h:8003/v1"}},
            },
        },
    }
    caps = {"stt_engines": [{"name": "cohere", "up": True}]}

    assert available_remote_slots(cfg, caps) == 4


def test_available_remote_slots_limits_cold_or_starting_stt_engine():
    cfg = {
        "models": {"stt_backend": "cohere"},
        "inference": {"mode": "remote", "stt": {"concurrency": 8, "backends": {"cohere": {"url": "http://h/v1"}}}},
    }

    assert available_remote_slots(cfg, {"stt_engines": [{"name": "cohere", "up": False}]}) == 1
    assert available_remote_slots(cfg, {"stt_engines": [{"name": "cohere", "up": False, "ensure_in_progress": True}]}) == 0


def test_available_remote_slots_takes_minimum_required_inprocess_capacity():
    cfg = {
        "models": {"stt_backend": "cohere", "diarization_backend": "remote"},
        "inference": {
            "mode": "remote",
            "url": "http://node:8002",
            "stt": {"concurrency": 4, "backends": {"cohere": {"url": "http://h/v1"}}},
        },
    }
    caps = {
        "stt_engines": [{"name": "cohere", "up": True}],
        "inprocess": [
            {"name": "diarize", "capacity": 1, "inflight": 1, "queued": 0},
            {"name": "voice-embed", "capacity": 2, "inflight": 0, "queued": 0},
        ],
    }

    assert available_remote_slots(cfg, caps) == 0


def test_remote_vram_admits_remote_phase_when_gpu_has_headroom():
    cfg = {
        "gpu": {"min_free_vram_mb": 1000},
        "models": {"stt_backend": "cohere"},
        "inference": {"mode": "remote", "stt": {"backends": {"cohere": {"url": "http://h/v1"}}}},
    }
    profile = {"phases": {"stt": 6000, "diarization": 2000}}
    caps = {"gpus": [{"index": 0, "free_mb": 7500, "total_mb": 24000}]}

    assert remote_vram_admits(cfg, caps, profile) is True


def test_remote_vram_rejects_when_no_gpu_has_enough_free_vram():
    cfg = {
        "gpu": {"min_free_vram_mb": 1000},
        "models": {"stt_backend": "cohere"},
        "inference": {"mode": "remote", "stt": {"backends": {"cohere": {"url": "http://h/v1"}}}},
    }
    profile = {"phases": {"stt": 6000}}
    caps = {"gpus": [{"index": 0, "free_mb": 6500, "total_mb": 24000}]}

    assert remote_vram_admits(cfg, caps, profile) is False


def test_remote_vram_ignores_local_only_phase_costs():
    cfg = {
        "gpu": {"min_free_vram_mb": 1000},
        "models": {"stt_backend": "cohere", "diarization_backend": "pyannote"},
        "inference": {"mode": "remote", "stt": {"backends": {"cohere": {"url": "http://h/v1"}}}},
    }
    profile = {"phases": {"stt": 4000, "diarization": 60000}}
    caps = {"gpus": [{"index": 0, "free_mb": 5500, "total_mb": 24000}]}

    assert remote_vram_admits(cfg, caps, profile) is True


# ── InferenceClient.capabilities() ────────────────────────────────────────────

class _Resp:
    def __init__(self, code, payload=None):
        self.status_code = code
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


class _Session:
    def __init__(self, resp):
        self._resp = resp

    def get(self, url, headers=None, timeout=None):
        if isinstance(self._resp, Exception):
            raise self._resp
        return self._resp


def test_client_capabilities_ok():
    client = InferenceClient("http://h:8002", session=_Session(_Resp(200, {"deployment_mode": "x"})))
    assert client.capabilities() == {"deployment_mode": "x"}


def test_client_capabilities_unreachable():
    import requests

    client = InferenceClient("http://h:8002", session=_Session(requests.exceptions.ConnectionError("x")))
    with pytest.raises(InferenceUnavailable):
        client.capabilities()

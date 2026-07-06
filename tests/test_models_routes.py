"""Routes admin « Modèles » : accès, rendu, garde token gated, check espace, progression JSON."""
from __future__ import annotations

_VRAM = "transcria.services.config_service.ConfigService.detect_system"


def _no_detect(monkeypatch, vram_mb=24000):
    monkeypatch.setattr(_VRAM, lambda: {"total_vram_mb": vram_mb})  # évite nvidia-smi en test


def test_models_page_forbidden_for_viewer(viewer_client):
    assert viewer_client.get("/admin/models").status_code == 403


def test_models_page_renders_for_admin(admin_client, monkeypatch):
    _no_detect(monkeypatch)
    resp = admin_client.get("/admin/models")
    assert resp.status_code == 200
    assert "Modèles" in resp.get_data(as_text=True)


def test_models_download_starts_for_non_gated_llm(admin_client, monkeypatch):
    _no_detect(monkeypatch)
    monkeypatch.setattr("transcria.models_download.check_space", lambda spec, **_k: (True, "ok"))
    called: dict = {}
    monkeypatch.setattr("transcria.models_download.start_download",
                        lambda spec, token=None, **_k: called.update(role=spec.role, gated=spec.gated))
    resp = admin_client.post("/admin/models/download", data={"role": "arbitrage_llm"})
    assert resp.status_code == 302
    assert called == {"role": "arbitrage_llm", "gated": False}  # LLM GGUF = sans token


def test_models_download_gated_requires_token(admin_client, monkeypatch):
    _no_detect(monkeypatch)
    monkeypatch.delenv("HF_TOKEN", raising=False)  # pas de token configuré
    called: dict = {}
    monkeypatch.setattr("transcria.models_download.start_download", lambda spec, **_k: called.update(hit=1))
    resp = admin_client.post("/admin/models/download", data={"role": "stt"})  # cohere = gated
    assert resp.status_code == 302
    assert "hit" not in called  # gated sans token → refusé, aucun téléchargement


def test_models_download_refuses_when_no_space(admin_client, monkeypatch):
    _no_detect(monkeypatch)
    monkeypatch.setattr("transcria.models_download.check_space", lambda spec, **_k: (False, "espace insuffisant : 0 Go"))
    called: dict = {}
    monkeypatch.setattr("transcria.models_download.start_download", lambda spec, **_k: called.update(hit=1))
    resp = admin_client.post("/admin/models/download", data={"role": "arbitrage_llm"})
    assert resp.status_code == 302
    assert "hit" not in called


def test_models_download_unknown_role_404(admin_client, monkeypatch):
    _no_detect(monkeypatch)
    assert admin_client.post("/admin/models/download", data={"role": "nope"}).status_code == 404


def test_models_progress_returns_json(admin_client):
    resp = admin_client.get("/admin/models/progress/arbitrage_llm")
    assert resp.status_code == 200
    assert resp.get_json()["status"] in ("absent", "starting", "downloading", "done", "error")


def test_models_download_forbidden_for_viewer(viewer_client):
    assert viewer_client.post("/admin/models/download", data={}).status_code == 403


def test_models_activate_switches_profile_when_present(admin_client, monkeypatch):
    import subprocess

    _no_detect(monkeypatch)
    monkeypatch.setattr("transcria.models_catalog.model_status",
                        lambda spec, **_k: {"present": True, "path": "/x", "size_bytes": 1})
    calls: dict = {}

    def fake_run(cmd, **_kw):
        calls["cmd"] = cmd

        class _R:
            returncode, stdout, stderr = 0, "ok", ""

        return _R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    resp = admin_client.post("/admin/models/activate")
    assert resp.status_code == 302
    assert calls["cmd"][:2] == ["bash", "scripts/switch_arbitrage_llm.sh"]
    assert calls["cmd"][2].endswith("gb")  # ex. "24gb"


def test_models_activate_requires_present(admin_client, monkeypatch):
    import subprocess

    _no_detect(monkeypatch)
    monkeypatch.setattr("transcria.models_catalog.model_status",
                        lambda spec, **_k: {"present": False, "path": None, "size_bytes": 0})
    called: dict = {}
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.update(hit=1))
    resp = admin_client.post("/admin/models/activate")
    assert resp.status_code == 302
    assert "hit" not in called  # pas téléchargé → pas de bascule


def test_models_activate_forbidden_for_viewer(viewer_client):
    assert viewer_client.post("/admin/models/activate").status_code == 403

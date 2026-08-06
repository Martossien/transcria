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


class TestOllamaRendering:
    """Backend Ollama : rendu des branches spécifiques (badge démon, pas de bouton
    « Activer (servir) » — il bascule un profil llama.cpp, hors sujet pour Ollama)."""

    def _view(self, present, daemon_up):
        from transcria.models_catalog import ModelSpec
        spec = ModelSpec("arbitrage_llm", "LLM d'arbitrage (Ollama : qwen3.6:27b)",
                         "qwen3.6:27b", None, "ollama", "", False, "lic", "u", 0.0)
        return {"items": [{"spec": spec, "present": present, "path": None,
                           "size_bytes": 17_000_000_000 if present else 0,
                           "daemon_up": daemon_up, "progress": {"status": "absent"}}],
                "hf_home": "/hf", "models_dir": "/m", "hf_free_gb": 100.0, "models_free_gb": 100.0}

    def _render(self, admin_client, monkeypatch, view, choices=None):
        _no_detect(monkeypatch)
        monkeypatch.setattr("transcria.web.admin_routes.catalog_with_status",
                            lambda cfg, total_vram_mb=None: view)
        # Liste de bascule contrôlée : pas de sonde GPU (inventory) ni de catalogue réel en test.
        monkeypatch.setattr("transcria.web.admin_routes._ollama_choices",
                            lambda v: choices or [])
        resp = admin_client.get("/admin/models")
        assert resp.status_code == 200
        return resp.get_data(as_text=True)

    def test_present_sans_bouton_activer(self, admin_client, monkeypatch):
        body = self._render(admin_client, monkeypatch, self._view(present=True, daemon_up=True))
        assert "qwen3.6:27b" in body and "présent" in body
        assert "Activer (servir)" not in body

    def test_absent_daemon_up_offre_le_pull(self, admin_client, monkeypatch):
        body = self._render(admin_client, monkeypatch, self._view(present=False, daemon_up=True))
        assert "Télécharger" in body
        assert "magasin Ollama" in body and "~" not in body.split("magasin Ollama")[0][-200:]

    def test_daemon_injoignable_message_sans_pull(self, admin_client, monkeypatch):
        body = self._render(admin_client, monkeypatch, self._view(present=False, daemon_up=False))
        assert "démon Ollama injoignable" in body
        assert "Télécharger" not in body


class TestOllamaSwitch:
    """Bascule de modèle Ollama — symétrique du « Activer (servir) » GGUF."""

    def _choices(self):
        return [
            {"model": "qwen3.5:9b", "context": 262144, "pulled": True,
             "size_bytes": 6_000_000_000, "recommended": False, "active": True},
            {"model": "qwen3.6:27b", "context": 262144, "pulled": False,
             "size_bytes": 0, "recommended": True, "active": False},
        ]

    def _setup(self, monkeypatch, saved: dict, pulls: list):
        monkeypatch.setattr("transcria.web.admin_routes._models_view",
                            lambda: {"items": []})
        monkeypatch.setattr("transcria.web.admin_routes._ollama_choices",
                            lambda v: self._choices())
        def fake_save(cfg, config_path=None):
            saved.update(cfg)
            return True, [], []
        monkeypatch.setattr("transcria.services.config_service.ConfigService.save_if_valid",
                            staticmethod(fake_save))
        monkeypatch.setattr("transcria.llm_tools.opencode_setup.ensure_local_provider",
                            lambda path, base, model, **k: saved.setdefault("_opencode", model))
        monkeypatch.setattr("transcria.web.admin_routes.models_download.start_download",
                            lambda spec, **k: pulls.append((spec.kind, spec.repo_id)))

    def test_bascule_ecrit_les_memes_cles_que_l_install_et_pull_si_absent(self, admin_client, monkeypatch):
        saved: dict = {}
        pulls: list = []
        self._setup(monkeypatch, saved, pulls)
        resp = admin_client.post("/admin/models/ollama-activate", data={"model": "qwen3.6:27b"})
        assert resp.status_code == 302
        # Mêmes clés que la phase ollama d'install.sh (_write_backend_config).
        assert saved["services"]["ollama_model"] == "qwen3.6:27b"
        assert saved["workflow"]["summary_llm"]["model_id"] == "local/qwen3.6:27b"
        assert saved["workflow"]["arbitration_llm"]["model_id"] == "local/qwen3.6:27b"
        assert saved["_opencode"] == "qwen3.6:27b"           # provider opencode réaligné
        assert pulls == [("ollama", "qwen3.6:27b")]          # absent → pull en arrière-plan

    def test_modele_deja_tire_pas_de_pull(self, admin_client, monkeypatch):
        saved: dict = {}
        pulls: list = []
        self._setup(monkeypatch, saved, pulls)
        monkeypatch.setattr("transcria.web.admin_routes._ollama_choices",
                            lambda v: [{**self._choices()[1], "pulled": True, "size_bytes": 1}])
        admin_client.post("/admin/models/ollama-activate", data={"model": "qwen3.6:27b"})
        assert pulls == [] and saved["services"]["ollama_model"] == "qwen3.6:27b"

    def test_modele_hors_choix_refuse_sans_effet(self, admin_client, monkeypatch):
        # La valeur part dans config.yaml ET l'argv d'ollama pull : jamais de valeur libre.
        saved: dict = {}
        pulls: list = []
        self._setup(monkeypatch, saved, pulls)
        resp = admin_client.post("/admin/models/ollama-activate", data={"model": "evil; rm -rf"})
        assert resp.status_code == 302
        assert saved == {} and pulls == []

    def test_modele_deja_actif_sans_effet(self, admin_client, monkeypatch):
        saved: dict = {}
        pulls: list = []
        self._setup(monkeypatch, saved, pulls)
        admin_client.post("/admin/models/ollama-activate", data={"model": "qwen3.5:9b"})
        assert saved == {} and pulls == []

    def test_config_invalide_bascule_refusee(self, admin_client, monkeypatch):
        saved: dict = {}
        pulls: list = []
        self._setup(monkeypatch, saved, pulls)
        monkeypatch.setattr("transcria.services.config_service.ConfigService.save_if_valid",
                            staticmethod(lambda cfg, config_path=None: (False, ["clé invalide"], [])))
        admin_client.post("/admin/models/ollama-activate", data={"model": "qwen3.6:27b"})
        assert pulls == [] and "_opencode" not in saved      # rien après le refus

    def test_bloc_de_bascule_rendu(self, admin_client, monkeypatch):
        rendering = TestOllamaRendering()
        body = rendering._render(admin_client, monkeypatch,
                                 rendering._view(present=True, daemon_up=True),
                                 choices=self._choices())
        assert "changer de modèle" in body
        assert "Utiliser ce modèle" in body
        assert "recommandé pour cette machine" in body
        assert "qwen3.6:27b" in body

"""Checklist de premier démarrage — bilan (transcria/web/first_run) et route accueil.

Le bilan compose les MÊMES sources que les pages de réparation (catalogue de la page
Modèles, checks doctor légers) ; ces tests substituent ces sources par module
(patron du projet : accès par module, jamais `from … import fonction`).
"""
from __future__ import annotations

from types import SimpleNamespace

from transcria.web import first_run


def _catalog(items):
    return lambda cfg, total_vram_mb=None: {"items": items}


def _gpu(total_gib=24.0):
    return SimpleNamespace(id=0, name="RTX", used_gib=1.0, free_gib=23.0, total_gib=total_gib)


class TestComputeItem:
    def test_gpu_locaux_ok(self, monkeypatch):
        monkeypatch.setattr(first_run.inventory, "snapshot", lambda: [_gpu(), _gpu()])
        item = first_run._compute_item({})
        assert item.status == "ok"
        assert item.data == {"gpus": 2, "vram_gib": 48.0}

    def test_aucun_gpu_warn(self, monkeypatch):
        monkeypatch.setattr(first_run.inventory, "snapshot", lambda: [])
        item = first_run._compute_item({})
        assert item.status == "warn" and item.data == {"gpus": 0}

    def test_mode_distant_delegue_au_check_des_noeuds(self, monkeypatch):
        # En topologie déportée, ce sont les nœuds qui comptent, pas le GPU local.
        called = {}

        def fake_check(cfg):
            called["cfg"] = cfg
            return SimpleNamespace(status="fail", detail="0 nœud joignable")

        monkeypatch.setattr(first_run, "check_inference_nodes", fake_check)
        monkeypatch.setattr(first_run.inventory, "snapshot",
                            lambda: (_ for _ in ()).throw(AssertionError("GPU local hors sujet en remote")))
        item = first_run._compute_item({"inference": {"mode": "remote"}})
        assert item.status == "fail"
        assert item.data["mode"] == "remote" and "cfg" in called


class TestModelsItem:
    def test_tout_present(self, monkeypatch):
        monkeypatch.setattr(first_run, "catalog_with_status", _catalog([
            {"spec": SimpleNamespace(label="STT Whisper"), "present": True},
        ]))
        item = first_run._models_item({}, None)
        assert item.status == "ok" and item.data == {"total": 1}

    def test_manquants_listes(self, monkeypatch):
        monkeypatch.setattr(first_run, "catalog_with_status", _catalog([
            {"spec": SimpleNamespace(label="STT Whisper"), "present": False},
            {"spec": SimpleNamespace(label="Diarisation"), "present": True},
            {"spec": SimpleNamespace(label="LLM d'arbitrage"), "present": False},
        ]))
        item = first_run._models_item({}, 24000)
        assert item.status == "warn"
        assert item.data == {"missing": ["STT Whisper", "LLM d'arbitrage"]}


class TestReport:
    def test_needs_attention_filtre_les_verts(self):
        items = [
            first_run.FirstRunItem("compute", "ok"),
            first_run.FirstRunItem("models", "warn"),
            first_run.FirstRunItem("opencode", "fail"),
        ]
        assert [it.key for it in first_run.needs_attention(items)] == ["models", "opencode"]

    def test_report_complet(self, monkeypatch):
        monkeypatch.setattr(first_run.inventory, "snapshot", lambda: [_gpu()])
        monkeypatch.setattr(first_run, "catalog_with_status", _catalog([]))
        monkeypatch.setattr(first_run, "check_opencode",
                            lambda cfg: SimpleNamespace(status="ok", detail="trouvé"))
        report = first_run.first_run_report({}, total_vram_mb=24000)
        assert [it.key for it in report] == ["compute", "models", "opencode"]
        assert first_run.needs_attention(report) == []


class TestRoute:
    _VRAM = "transcria.services.config_service.ConfigService.detect_system"

    def test_interdit_aux_non_admins(self, viewer_client):
        assert viewer_client.get("/admin/first-run-status").status_code == 403

    def test_tout_vert_repond_204(self, admin_client, monkeypatch):
        monkeypatch.setattr(self._VRAM, lambda: {"total_vram_mb": 24000})
        monkeypatch.setattr("transcria.web.first_run.first_run_report",
                            lambda cfg, total_vram_mb=None: [first_run.FirstRunItem("compute", "ok")])
        resp = admin_client.get("/admin/first-run-status")
        assert resp.status_code == 204 and resp.get_data() == b""

    def test_manques_rendus_avec_lien_vers_la_page_modeles(self, admin_client, monkeypatch):
        monkeypatch.setattr(self._VRAM, lambda: {"total_vram_mb": 24000})
        monkeypatch.setattr("transcria.web.first_run.first_run_report",
                            lambda cfg, total_vram_mb=None: [
                                first_run.FirstRunItem("compute", "warn", {"gpus": 0}),
                                first_run.FirstRunItem("models", "warn", {"missing": ["STT Whisper"]}),
                                first_run.FirstRunItem("opencode", "fail", {"detail": "introuvable"}),
                            ])
        resp = admin_client.get("/admin/first-run-status")
        body = resp.get_data(as_text=True)
        assert resp.status_code == 200
        assert "STT Whisper" in body and "/admin/models" in body
        assert "Aucun GPU" in body and "opencode" in body

    def test_accueil_admin_porte_le_conteneur(self, admin_client):
        body = admin_client.get("/").get_data(as_text=True)
        assert 'id="first-run-checklist"' in body
        assert "/admin/first-run-status" in body

    def test_accueil_viewer_sans_conteneur(self, viewer_client):
        body = viewer_client.get("/").get_data(as_text=True)
        assert 'id="first-run-checklist"' not in body

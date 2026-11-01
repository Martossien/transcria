"""LLM d'arbitrage cloud — lot C : gardes UI / provisioning / modèle de temps (docs/LLM_CLOUD.md § 3.3.2).

Chaque garde est testée contre la config cloud de référence ; hors cloud, le comportement
historique reste inchangé (tests existants des modules concernés).
"""
from __future__ import annotations

import copy
import dataclasses
import threading
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from builders import make_config

from transcria.config import _deep_merge
from transcria.gpu import hardware_advisor
from transcria.models_catalog import build_catalog, catalog_with_status
from transcria.notifications import mailer
from transcria.web import first_run
from transcria.workflow import timing_model

CLOUD = {
    "services": {"backend": "cloud"},
    "workflow": {
        "arbitration_llm": {"enabled": True, "model_id": "mistral/mistral-large-4"},
        "summary_llm": {"enabled": True, "model_id": "mistral/mistral-large-4"},
    },
}

_CFG_SINGLETON = "transcria.web.admin_routes.ConfigService.get_singleton"
_VRAM = "transcria.services.config_service.ConfigService.detect_system"


def _cloud_cfg() -> dict:
    return make_config(CLOUD)


def _app_cloud_cfg() -> dict:
    """Config de l'app de test + bascule cloud (les routes rendent base.html)."""
    from transcria.services.config_service import ConfigService

    return _deep_merge(copy.deepcopy(ConfigService.get_singleton()), CLOUD)


# --- 1. Bascules de modèle refusées en cloud -------------------------------------------

class TestBasculesRefuseesEnCloud:
    def test_bascule_gguf_refusee_sans_script(self, admin_client, monkeypatch):
        cfg = _app_cloud_cfg()
        monkeypatch.setattr(_CFG_SINGLETON, lambda: cfg)
        called = {}
        monkeypatch.setattr("transcria.web.admin_routes.subprocess.run",
                            lambda *a, **k: called.update(run=1))
        resp = admin_client.post("/admin/models/activate")
        assert resp.status_code == 302
        assert not called
        monkeypatch.setattr(_VRAM, lambda: {"total_vram_mb": 64000})
        assert "fournisseur cloud : changez de modèle" in admin_client.get("/admin/models").get_data(as_text=True)

    def test_bascule_ollama_refusee_sans_opencode_ni_config(self, admin_client, monkeypatch):
        cfg = _app_cloud_cfg()
        monkeypatch.setattr(_CFG_SINGLETON, lambda: cfg)
        called = {}
        monkeypatch.setattr("transcria.llm_tools.opencode_setup.ensure_local_provider",
                            lambda *a, **k: called.update(opencode=1))
        monkeypatch.setattr("transcria.services.config_service.ConfigService.save_if_valid",
                            lambda *a, **k: called.update(save=1) or (True, [], []))
        monkeypatch.setattr("transcria.web.admin_routes.models_download.start_download",
                            lambda *a, **k: called.update(pull=1))
        resp = admin_client.post("/admin/models/ollama-activate", data={"model": "qwen3.6:27b"},
                                 follow_redirects=True)
        assert resp.status_code == 200
        assert "ne pilote que la LLM locale" in resp.get_data(as_text=True)
        assert called == {}

    def test_telechargement_d_un_modele_cloud_refuse(self, admin_client, monkeypatch):
        cfg = _app_cloud_cfg()
        monkeypatch.setattr(_CFG_SINGLETON, lambda: cfg)
        monkeypatch.setattr(_VRAM, lambda: {"total_vram_mb": 64000})
        called = {}
        monkeypatch.setattr("transcria.web.admin_routes.models_download.start_download",
                            lambda *a, **k: called.update(start=1))
        monkeypatch.setattr("transcria.web.admin_routes.models_download.check_space",
                            lambda *a, **k: called.update(space=1) or (True, ""))
        resp = admin_client.post("/admin/models/download", data={"role": "arbitrage_llm"},
                                 follow_redirects=True)
        assert resp.status_code == 200
        assert "Ce modèle est servi par un fournisseur cloud, rien à télécharger" in resp.get_data(as_text=True)
        assert called == {}

    def test_start_download_refuse_un_modele_cloud(self):
        from transcria import models_download

        spec = next(s for s in build_catalog(_cloud_cfg(), total_vram_mb=64000) if s.kind == "cloud")
        spawned = []
        with pytest.raises(ValueError, match="rien à télécharger"):
            models_download.start_download(spec, popen=lambda *a, **k: spawned.append(a))
        assert spawned == []


# --- 2. Page Modèles : ligne d'information cloud ---------------------------------------

class TestCatalogueCloud:
    def test_ligne_cloud_sans_gguf(self):
        specs = build_catalog(_cloud_cfg(), total_vram_mb=64000)
        llm = [s for s in specs if s.role == "arbitrage_llm"]
        assert len(llm) == 1
        assert llm[0].kind == "cloud" and llm[0].file is None
        assert "mistral" in llm[0].label and "mistral-large-4" in llm[0].label
        assert not [s for s in specs if s.role == "arbitrage_llm" and s.kind == "gguf"]

    def test_ligne_cloud_jamais_un_manque(self):
        view = catalog_with_status(_cloud_cfg(), total_vram_mb=64000)
        item = next(it for it in view["items"] if it["spec"].kind == "cloud")
        assert item["present"] is True and item.get("optional") is True

    def test_page_affiche_la_ligne_et_le_lien(self, admin_client, monkeypatch):
        cfg = _app_cloud_cfg()
        monkeypatch.setattr(_CFG_SINGLETON, lambda: cfg)
        monkeypatch.setattr(_VRAM, lambda: {"total_vram_mb": 64000})
        html = admin_client.get("/admin/models").get_data(as_text=True)
        assert "LLM d&#39;arbitrage (cloud : mistral / mistral-large-4)" in html or \
            "LLM d'arbitrage (cloud : mistral / mistral-large-4)" in html
        assert "/admin/llm-cloud" in html
        assert "/admin/models/activate" not in html


# --- 3. Bilan de premier démarrage -----------------------------------------------------

class TestFirstRunCloud:
    def test_compute_sans_gpu_ok_en_cloud_palier_zero_gpu(self, monkeypatch):
        """Zéro GPU + LLM cloud = configuration voulue : l'item ne doit pas maintenir la
        checklist d'accueil affichée à vie."""
        monkeypatch.setattr(first_run.inventory, "snapshot", lambda: [])
        item = first_run._compute_item(_cloud_cfg(), {"active": True})
        assert item.status == "ok" and item.data == {"gpus": 0, "llm_cloud": True}
        assert first_run.needs_attention([item]) == []
        # Hors cloud, l'absence de GPU reste un point à finaliser.
        assert first_run._compute_item(make_config(), {"active": False}).status == "warn"

    def test_item_llm_cloud_absent_hors_cloud(self):
        assert first_run._llm_cloud_item({"active": False}) is None

    def test_item_llm_cloud_warn_si_non_valide_ok_sinon(self):
        base = {"active": True, "provider_label": "Mistral", "model": "mistral-large-4"}
        assert first_run._llm_cloud_item({**base, "acknowledged": False}).status == "warn"
        assert first_run._llm_cloud_item({**base, "acknowledged": True}).status == "ok"

    def test_rapport_cloud_sans_gguf_et_avec_llm_cloud(self, app, monkeypatch):
        monkeypatch.setattr(first_run.inventory, "snapshot", lambda: [])
        monkeypatch.setattr(first_run, "check_opencode",
                            lambda cfg: SimpleNamespace(status="ok", detail=""))
        with app.app_context():
            items = {it.key: it for it in first_run.first_run_report(_cloud_cfg(), total_vram_mb=64000)}
        # Aucune validation en base : WARN (traitements LLM refusés).
        assert items["llm_cloud"].status == "warn"
        assert items["compute"].data.get("llm_cloud") is True
        assert items["compute"].status == "ok"
        missing = items["models"].data.get("missing") or []
        assert not [m for m in missing if "arbitrage" in m]

    def test_rapport_hors_cloud_sans_item_llm_cloud(self, monkeypatch):
        monkeypatch.setattr(first_run.inventory, "snapshot", lambda: [])
        items = first_run.first_run_report(make_config())
        assert "llm_cloud" not in {it.key for it in items}

    def test_fragment_rend_le_lien_de_validation(self, admin_client, monkeypatch):
        items = [
            first_run.FirstRunItem("compute", "ok", {"gpus": 0, "llm_cloud": True}),
            first_run.FirstRunItem("llm_cloud", "warn", {"provider": "Mistral", "model": "mistral-large-4"}),
        ]
        monkeypatch.setattr("transcria.web.admin_routes.first_run.first_run_report",
                            lambda cfg, total_vram_mb=None: items)
        monkeypatch.setattr(_VRAM, lambda: {"total_vram_mb": 0})
        html = admin_client.get("/admin/first-run-status").get_data(as_text=True)
        assert "non validée par un administrateur" in html
        assert "/admin/llm-cloud" in html
        # Zéro GPU en cloud : compute OK, donc aucune ligne « aucun GPU » dans le fragment.
        assert "Aucun GPU détecté" not in html


# --- 4. Conseiller matériel ------------------------------------------------------------

def test_llm_tier_card_masquee_en_cloud():
    totals = {0: 24576, 1: 24576}
    assert hardware_advisor.llm_tier_card(_cloud_cfg(), totals) is None
    cards, _ = hardware_advisor.build_advice(_cloud_cfg(), gpu_totals_provider=lambda: totals)
    assert "llm_tier" not in {c.kind for c in cards}


# --- 5. E-mail « attente de VRAM » -----------------------------------------------------

def _vram_cfg(extra: dict | None = None) -> dict:
    cfg = {"notifications": {"email": {"enabled": True, "smtp_host": "smtp.test", "from_address": "t@x.com",
                                       "base_url": "http://localhost"}},
           "i18n": {"default_locale": "fr"}}
    return _deep_merge(cfg, extra or {})


def _sent_html(cfg: dict) -> str:
    sent: list[str] = []
    done = threading.Event()

    def fake_smtp(ecfg, to, subject, html, text):
        sent.append(html)
        done.set()

    with patch("transcria.notifications.mailer._send_smtp", side_effect=fake_smtp):
        mailer.send_admin_vram_alert_async(cfg, ["a@x.com"], "Réunion", "job1", 6000, "transcription")
        assert done.wait(timeout=2)
    return sent[0]


def test_mail_vram_sans_arret_de_llm_en_cloud():
    assert "arrêt d'une LLM" not in _sent_html(_vram_cfg(CLOUD))


def test_mail_vram_local_conseille_toujours_l_arret():
    assert "arrêt d'une LLM" in _sent_html(_vram_cfg())


# --- 6. Modèle de temps : clé distincte en cloud ---------------------------------------

def test_stage_key_suffixe_les_seules_etapes_llm():
    assert timing_model.stage_key("correction", cloud=True) == "correction@cloud"
    assert timing_model.stage_key("summary", cloud=True) == "summary@cloud"
    assert timing_model.stage_key("transcribe", cloud=True) == "transcribe"
    assert timing_model.stage_key("correction", cloud=False) == "correction"
    assert timing_model.timing_stage_key("final_review", _cloud_cfg()) == "final_review@cloud"
    assert timing_model.timing_stage_key("final_review", None) == "final_review"


@pytest.fixture
def _clean_timings(app):
    """Historique de durées vidé avant ET après (patron de tests/test_timing_store.py) :
    ces tests ne dépendent ni de l'ordre ni des échantillons laissés par d'autres."""
    from transcria.database import db
    from transcria.jobs.timing_store import JobTiming

    def _purge() -> None:
        with app.app_context():
            db.session.query(JobTiming).delete()
            db.session.commit()

    _purge()
    yield
    _purge()


def _unique_profile_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def test_enregistrement_et_lecture_separes_local_cloud(app, _clean_timings):
    from transcria.jobs.timing_store import JobTimingStore

    pid = _unique_profile_id("lot_c_prof")
    local, cloud = make_config(), _cloud_cfg()
    with app.app_context():
        JobTimingStore.record(pid, "correction", 600, 300, config=local)
        JobTimingStore.record(pid, "correction", 600, 30, config=cloud)
        JobTimingStore.record(pid, "transcribe", 600, 60, config=cloud)
        assert JobTimingStore.recent_samples(pid, "correction", config=local) == [(600.0, 300.0)]
        assert JobTimingStore.recent_samples(pid, "correction", config=cloud) == [(600.0, 30.0)]
        # Étape non LLM : clé partagée (la vitesse du STT ne dépend pas du fournisseur LLM).
        assert JobTimingStore.recent_samples(pid, "transcribe", config=local) == [(600.0, 60.0)]
        assert set(JobTimingStore.samples_for_stages(pid, ["correction"], config=cloud)) == {"correction"}


def test_estimation_cloud_sans_echantillon_retombe_sur_le_defaut(app, _clean_timings):
    from transcria.jobs.timing_store import JobTimingStore
    from transcria.workflow import profiles
    from transcria.workflow.timing_service import estimate_processing, processing_stages

    base = next(p for p in profiles.list_profiles() if "correction" in processing_stages(p))
    prof = dataclasses.replace(base, id=_unique_profile_id(base.id))
    with app.app_context():
        for audio in (300, 600, 900, 1200, 1500, 1800):
            for st in processing_stages(prof):
                JobTimingStore.record(prof.id, st, audio, 0.1 * audio, config=make_config())
        assert estimate_processing(prof, 900, config=make_config()).basis == "measured"
        # En cloud, la correction n'a aucun point : formule, pas les points de la LLM locale.
        assert estimate_processing(prof, 900, config=_cloud_cfg()).basis == "initial"


def test_multi_stt_review_est_une_etape_llm():
    """L'arbitrage multi-STT appelle la LLM directement : clé distincte en cloud."""
    assert timing_model.stage_key("multi_stt_review", cloud=True) == "multi_stt_review@cloud"

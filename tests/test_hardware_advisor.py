"""Conseiller matériel — cartes pures + route admin (GET/POST apply)."""
from __future__ import annotations

from transcria.gpu.hardware_advisor import build_advice, concurrency_card, stt_instances_card

_CFG_SERVED = {
    "inference": {"stt": {"backends": {"qwen3asr": {"url": "http://127.0.0.1:8021/v1"}}}},
    "resource_node": {"engines": [
        {"name": "qwen3asr", "script": "scripts/launch_stt_qwen3asr.sh", "gpu": 1, "port": 8021},
    ]},
}


def test_pc_upgrade_propose_le_plan_applicable():
    """Le cas moteur du lot : 2× 24 Go sans LLM → 3 instances proposées, applicable."""
    card = stt_instances_card(_CFG_SERVED, {0: 24576, 1: 24576})
    assert card is not None and card.status == "improve" and card.applicable
    payload = card.apply_payload
    assert len(payload["engines"]) == 3
    assert payload["concurrency"] == 6
    assert payload["engines"][0]["name"] == "qwen3asr"


def test_machine_saturee_par_la_llm_reste_ok():
    cfg = {**_CFG_SERVED, "gpu": {"llm_gpu_indices": [0, 1], "llm_vram_mb_per_gpu": [26000, 23000]}}
    card = stt_instances_card(cfg, {0: 32607, 1: 32607})
    assert card is not None and card.status == "ok" and not card.applicable


def test_gpu_heterogenes_plan_par_carte():
    """Parc mixte (12 Go + 24 Go) : le plan remplit la plus libre d'abord."""
    card = stt_instances_card(_CFG_SERVED, {0: 12288, 1: 24576})
    assert card is not None and card.applicable
    gpus = [e["gpu"] for e in card.apply_payload["engines"]]
    assert gpus[0] == 1  # la 24 Go d'abord


def test_sans_backend_servi_aucune_carte_stt():
    cards, _ = build_advice({"inference": {"stt": {"backends": {}}}},
                            gpu_totals_provider=lambda: {0: 24576})
    assert not any(c.kind == "stt_instances" for c in cards)


def test_sans_gpu_page_degradee():
    cards, totals = build_advice(_CFG_SERVED, gpu_totals_provider=lambda: {})
    assert totals == {}
    assert not any(c.kind == "llm_tier" for c in cards)


def test_concurrency_card_alerte_si_sous_dimensionnee():
    cfg = {"inference": {"stt": {"concurrency": 1, "backends": {"qwen3asr": {
        "url": "http://127.0.0.1:8021/v1", "extra_urls": ["http://127.0.0.1:8022/v1"]}}}}}
    card = concurrency_card(cfg)
    assert card is not None and card.status == "improve"
    assert "concurrency 4" in card.recommended


# ── Route /admin/hardware (client de test, sonde GPU monkeypatchée) ─────────────


def test_admin_hardware_get_rend_les_cartes(admin_client, monkeypatch):
    monkeypatch.setattr("transcria.gpu.hardware_advisor._detect_gpu_totals_mb",
                        lambda: {0: 24576, 1: 24576})
    r = admin_client.get("/admin/hardware")
    assert r.status_code == 200
    body = r.data.decode()
    assert "Préconisations matériel" in body
    assert "GPU 0" in body and "GPU 1" in body


def test_admin_hardware_sans_gpu_message_degrade(admin_client, monkeypatch):
    monkeypatch.setattr("transcria.gpu.hardware_advisor._detect_gpu_totals_mb", lambda: {})
    r = admin_client.get("/admin/hardware")
    assert r.status_code == 200
    assert "Aucun GPU détecté" in r.data.decode()


def test_admin_hardware_apply_sans_plan_actualise_ne_casse_rien(admin_client, monkeypatch):
    """POST apply quand le plan n'est pas applicable → redirection + message, config intacte."""
    monkeypatch.setattr("transcria.gpu.hardware_advisor._detect_gpu_totals_mb", lambda: {})
    r = admin_client.post("/admin/hardware", data={"_action": "apply_stt"},
                          follow_redirects=True)
    assert r.status_code == 200
    assert "Rien à appliquer" in r.data.decode()

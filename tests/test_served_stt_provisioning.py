"""Activation en un clic des moteurs STT servis : conventions (source unique), plan pur, application
en mémoire et sur fichier (commentaires/propriétaire conservés), déclaration par l'installateur, doctor."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from transcria.config import served_stt_provisioning as prov
from transcria.config.yaml_file import get_yaml_value, load_yaml_file
from transcria.stt import served_runtimes as sr

LOCAL = {"models": {"stt_backend": "cohere"}, "inference": {"mode": "local"},
         "services": {"arbitrage_llm_port": 8080, "llm_cleanup_ports": [8000]}}


def test_conventions_distinctes_et_coherentes_avec_les_lanceurs():
    ports = [rt.port for rt in sr.SERVED_STT.values()]
    assert len(set(ports)) == len(ports)                          # jamais deux moteurs sur le même port
    for rt in sr.SERVED_STT.values():
        script = Path(rt.script)
        assert script.is_file(), rt.script
        head = script.read_text(encoding="utf-8")[:4000]
        assert f"STT_PORT={rt.port}" in head or f"{rt.port}" in head   # le port conventionnel est celui du lanceur
        assert rt.runtime in sr.RUNTIME_INSTALL_COMMAND
    assert sr.SERVED_STT["nemotron"].health_path == "/health"     # parakeet-server n'a pas de /v1/models


def test_moteurs_references_par_la_config():
    assert sr.referenced_served_backends({"models": {"stt_backend": "qwen3asr", "summary_stt_backend": "qwen3asr"}}) == ["qwen3asr"]
    assert sr.referenced_served_backends({"models": {"stt_backend": "cohere", "summary_stt_backend": "nemotron"}}) == ["nemotron"]
    assert sr.referenced_served_backends({"models": {"stt_backend": "cohere"}}) == []
    assert sr.is_served_backend("voxtralrt") and not sr.is_served_backend("whisper") and not sr.is_served_backend(None)


def test_plan_pose_route_et_moteur_depuis_les_conventions():
    p = prov.plan_served_backend(LOCAL, "qwen3asr", gpu_index=2)
    assert p.engine == {"name": "qwen3asr", "script": "scripts/launch_stt_qwen3asr.sh", "gpu": 2, "gpu_mem": 0.25,
                        "port": 8021, "idle_timeout_s": 900}
    assert p.spec == {"url": "http://127.0.0.1:8021/v1", "model": "qwen3-asr-1.7b", "response_format": "json",
                      "fallback_backend": "whisper"}
    assert p.inference_mode == "hybrid" and not p.engine_reused and not p.route_reused and p.warnings == ()
    n = prov.plan_served_backend(LOCAL, "nemotron", gpu_index=0)
    assert n.engine["health_path"] == "/health" and n.spec["fallback_backend"] == "parakeet"


def test_plan_conserve_ce_que_l_admin_a_deja_regle():
    cfg = {"models": {}, "inference": {"mode": "remote", "stt": {"backends": {
        "qwen3asr": {"url": "http://10.0.0.9:9000/v1", "model": "perso"}}}},
        "resource_node": {"engines": [{"name": "qwen3asr", "script": "x.sh", "gpu": 7, "gpu_mem": 0.5, "port": 9000}]}}
    p = prov.plan_served_backend(cfg, "qwen3asr", gpu_index=0)
    assert p.engine_reused and p.route_reused and p.inference_mode is None
    assert p.engine["gpu"] == 7 and p.engine["port"] == 9000
    assert p.spec["url"] == "http://10.0.0.9:9000/v1" and p.spec["model"] == "perso"
    assert p.spec["response_format"] == "json" and p.spec["fallback_backend"] == "whisper"   # complété seulement


def test_plan_evite_un_port_deja_pris():
    cfg = {"models": {}, "inference": {"mode": "local"}, "services": {"arbitrage_llm_port": 8021},
           "resource_node": {"engines": [{"name": "autre", "port": 8022}]}}
    p = prov.plan_served_backend(cfg, "qwen3asr", gpu_index=0)
    assert p.port == 8023 and p.spec["url"].endswith(":8023/v1") and any("8021" in w for w in p.warnings)


def test_plan_refuse_un_moteur_inconnu():
    with pytest.raises(KeyError):
        prov.plan_served_backend(LOCAL, "whisper", gpu_index=0)


def test_pick_gpu_prend_la_carte_la_plus_libre():
    states = [SimpleNamespace(id=0, free_gib=2.0), SimpleNamespace(id=1, free_gib=18.5), SimpleNamespace(id=2, free_gib=7.0)]
    assert prov.pick_gpu(states) == 1 and prov.pick_gpu([]) == 0


def test_application_en_memoire_ne_touche_pas_aux_autres_moteurs():
    cfg = dict(LOCAL, resource_node={"engines": [{"name": "cohere", "port": 8003}]})
    out = prov.apply_in_memory(cfg, prov.plan_served_backend(cfg, "qwen3asr", gpu_index=1))
    assert [e["name"] for e in out["resource_node"]["engines"]] == ["cohere", "qwen3asr"]
    assert out["inference"]["mode"] == "hybrid" and out["inference"]["stt"]["backends"]["qwen3asr"]["url"].endswith(":8021/v1")
    assert cfg["inference"] == {"mode": "local"}                   # l'original est intact


def test_application_sur_fichier_conserve_commentaires_et_mode(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("# en-tête à conserver\nmodels:\n  stt_backend: cohere   # commentaire de ligne\ninference:\n  mode: local\n",
                      encoding="utf-8")
    config.chmod(0o600)
    prov.apply_to_file(config, prov.plan_served_backend(load_yaml_file(config), "nemotron", gpu_index=3))
    text = config.read_text(encoding="utf-8")
    assert text.startswith("# en-tête à conserver") and "# commentaire de ligne" in text
    assert oct(config.stat().st_mode & 0o777) == "0o600"
    cfg = load_yaml_file(config)
    assert get_yaml_value(cfg, "inference.mode") == "hybrid"
    assert get_yaml_value(cfg, "inference.stt.backends.nemotron.url") == "http://127.0.0.1:8022/v1"
    assert cfg["resource_node"]["engines"][0]["name"] == "nemotron" and cfg["resource_node"]["engines"][0]["gpu"] == 3


def test_installateur_declare_le_moteur_sans_le_choisir(tmp_path, monkeypatch):
    from transcria.installer import served_stt_config
    from transcria.installer.console import Console

    monkeypatch.setattr(served_stt_config.inventory, "snapshot", lambda: (SimpleNamespace(id=1, free_gib=20.0),))
    config = tmp_path / "config.yaml"
    config.write_text("models:\n  stt_backend: cohere\ninference:\n  mode: local\n", encoding="utf-8")
    assert served_stt_config.declare_served_engine(config, "qwen3asr", Console()) is True
    cfg = load_yaml_file(config)
    assert get_yaml_value(cfg, "models.stt_backend") == "cohere"                       # jamais choisi d'office
    assert get_yaml_value(cfg, "inference.stt.backends.qwen3asr.url") == "http://127.0.0.1:8021/v1"
    assert cfg["resource_node"]["engines"][0]["gpu"] == 1
    assert served_stt_config.declare_served_engine(config, "qwen3asr", Console()) is True   # idempotent
    assert len(cfg["resource_node"]["engines"]) == 1
    assert served_stt_config.declare_served_engine(None, "qwen3asr", Console()) is False
    assert served_stt_config.declare_served_engine(tmp_path / "absent.yaml", "qwen3asr", Console()) is False


def test_doctor_route_et_moteur_coherents():
    from transcria.diagnostics import doctor as doc

    assert doc.check_served_stt_routing({"models": {"stt_backend": "cohere"}}).status == doc.OK
    ok = prov.apply_in_memory(dict(LOCAL, models={"stt_backend": "qwen3asr"}),
                              prov.plan_served_backend(LOCAL, "qwen3asr", gpu_index=0))
    assert doc.check_served_stt_routing(ok).status == doc.OK
    missing = {"models": {"stt_backend": "qwen3asr"}}
    assert doc.check_served_stt_routing(missing).status == doc.WARN
    no_engine = {"models": {"stt_backend": "qwen3asr"},
                 "inference": {"stt": {"backends": {"qwen3asr": {"url": "http://127.0.0.1:8021/v1"}}}}}
    res = doc.check_served_stt_routing(no_engine)
    assert res.status == doc.WARN and "rien ne le lancera" in res.detail
    bad_port = dict(no_engine, resource_node={"engines": [{"name": "qwen3asr", "port": 8099}]})
    assert "8099" in doc.check_served_stt_routing(bad_port).detail
    remote = {"models": {"summary_stt_backend": "nemotron"},
              "inference": {"stt": {"backends": {"nemotron": {"url": "http://10.0.0.5:8022/v1"}}}}}
    assert doc.check_served_stt_routing(remote).status == doc.OK                     # nœud distant : rien à lancer ici

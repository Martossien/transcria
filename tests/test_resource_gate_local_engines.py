"""Gate all-in-one : ensure EN PROCESS d'un moteur STT servi localement.

La couture est `prepare_remote_resources` quand il n'y a PAS de nœud de contrôle
(`client_factory` → None) : si le backend STT est routé loopback ET qu'un moteur
homonyme est déclaré dans `resource_node.engines`, le gate l'assure lui-même
(cycle A/B/C du superviseur) ; sinon comportement historique (proceed résilience).
"""
from types import SimpleNamespace

from transcria.inference.resource_gate import prepare_remote_resources


def _config(url: str, *, engine_name: str | None = "qwen3asr") -> dict:
    cfg = {
        "models": {"stt_backend": "qwen3asr"},
        "inference": {
            "mode": "hybrid",
            "stt": {"backends": {"qwen3asr": {"url": url, "model": "qwen3-asr-1.7b"}}},
        },
    }
    if engine_name:
        cfg["resource_node"] = {"engines": [{
            "name": engine_name, "script": "scripts/launch_stt_qwen3asr.sh",
            "gpu": 0, "port": 8021,
        }]}
    return cfg


class FakeSupervisor:
    def __init__(self, status="ready", gpu_index=0, reason=""):
        self.calls: list = []
        self._result = SimpleNamespace(
            status=status, gpu_index=gpu_index, reason=reason,
            ok=status in ("ready", "launched"),
        )

    def ensure_ready(self, spec):
        self.calls.append(spec)
        return self._result


def _gate(cfg, supervisor):
    return prepare_remote_resources(
        cfg,
        client_factory=lambda _cfg: None,          # pas de nœud de contrôle
        supervisor_factory=lambda _cfg: supervisor,
    )


def test_loopback_declare_assure_le_moteur():
    sup = FakeSupervisor(status="launched", gpu_index=2)
    verdict = _gate(_config("http://127.0.0.1:8021/v1"), sup)
    assert verdict.action == "proceed"
    assert len(sup.calls) == 1 and sup.calls[0].name == "qwen3asr"
    assert "launched" in verdict.reason


def test_url_distante_ne_declenche_pas_l_ensure():
    sup = FakeSupervisor()
    verdict = _gate(_config("http://192.168.1.59:8021/v1"), sup)
    assert verdict.action == "proceed"           # résilience au niveau requête
    assert sup.calls == []                        # comportement historique intact


def test_moteur_non_declare_proceed_historique():
    sup = FakeSupervisor()
    verdict = _gate(_config("http://127.0.0.1:8021/v1", engine_name=None), sup)
    assert verdict.action == "proceed"
    assert sup.calls == []


def test_busy_devient_defer_avec_retry():
    sup = FakeSupervisor(status="busy", reason="VRAM saturée")
    verdict = _gate(_config("http://localhost:8021/v1"), sup)
    assert verdict.action == "defer"
    assert verdict.retry_after_s > 0
    assert "busy" in verdict.reason


def test_exception_superviseur_devient_defer_jamais_fail():
    class Boom:
        def ensure_ready(self, spec):
            raise RuntimeError("nvml indisponible")

    verdict = prepare_remote_resources(
        _config("http://127.0.0.1:8021/v1"),
        client_factory=lambda _cfg: None,
        supervisor_factory=lambda _cfg: Boom(),
    )
    assert verdict.action == "defer"


def test_avec_noeud_de_controle_l_ensure_local_est_ignore():
    # client présent → chemin split historique (/engines/ensure côté nœud), l'ensure
    # local ne doit PAS doubler. On vérifie juste que le superviseur local n'est pas
    # consulté (le verdict dépend ensuite de la sonde du nœud, hors périmètre ici).
    sup = FakeSupervisor()

    class FakeClient:
        def capabilities(self):
            return {"gpus": []}

        def ensure_engine(self, engine):
            return {"status": "ready"}   # le NŒUD assure — pas le superviseur local

    prepare_remote_resources(
        _config("http://127.0.0.1:8021/v1"),
        client_factory=lambda _cfg: FakeClient(),
        supervisor_factory=lambda _cfg: sup,
    )
    assert sup.calls == []


# ── Backend du RÉSUMÉ ≠ backend principal (lot 2, models.summary_stt_backend) ─

def _config_summary_servi() -> dict:
    """Pipeline cohere NATIF + résumé qwen3asr servi en loopback : le gate doit
    assurer le moteur du RÉSUMÉ (sinon « connection refused » au premier wizard)."""
    return {
        "models": {"stt_backend": "cohere", "summary_stt_backend": "qwen3asr"},
        "inference": {
            "mode": "hybrid",
            "stt": {"backends": {"qwen3asr": {"url": "http://127.0.0.1:8021/v1",
                                              "model": "qwen3-asr-1.7b"}}},
        },
        "resource_node": {"engines": [{
            "name": "qwen3asr", "script": "scripts/launch_stt_qwen3asr.sh",
            "gpu": 0, "port": 8021,
        }]},
    }


def test_backend_resume_servi_est_assure_meme_si_le_principal_est_natif():
    supervisor = FakeSupervisor(status="launched")
    verdict = _gate(_config_summary_servi(), supervisor)

    assert verdict.action == "proceed"
    assert [s.name for s in supervisor.calls] == ["qwen3asr"]


def test_backend_resume_identique_au_principal_un_seul_ensure():
    cfg = _config("http://127.0.0.1:8021/v1")
    cfg["models"]["summary_stt_backend"] = "qwen3asr"  # identique → dédupliqué
    supervisor = FakeSupervisor()

    verdict = _gate(cfg, supervisor)

    assert verdict.action == "proceed"
    assert len(supervisor.calls) == 1


def test_resume_servi_en_echec_devient_defer():
    supervisor = FakeSupervisor(status="busy", reason="chargement en cours")
    verdict = _gate(_config_summary_servi(), supervisor)

    assert verdict.action == "defer"
    assert "qwen3asr" in verdict.reason


# ── Multi-instance (§2.9) : plusieurs moteurs pour un même backend ──────────────


class FakeSupervisorParMoteur:
    """Résultat scripté PAR NOM de moteur (multi-instance)."""

    def __init__(self, results: dict):
        self.calls: list = []
        self._results = results

    def ensure_ready(self, spec):
        self.calls.append(spec.name)
        status = self._results.get(spec.name, "ready")
        return SimpleNamespace(status=status, gpu_index=spec.gpu, reason="",
                               ok=status in ("ready", "launched"))


def _config_deux_instances(statuses: dict) -> tuple[dict, FakeSupervisorParMoteur]:
    cfg = _config("http://127.0.0.1:8021/v1")
    cfg["resource_node"]["engines"].append({
        "name": "qwen3asr-gpu0", "backend": "qwen3asr",
        "script": "scripts/launch_stt_qwen3asr.sh", "gpu": 1, "port": 8022,
    })
    return cfg, FakeSupervisorParMoteur(statuses)


def test_multi_instance_les_deux_assurees():
    cfg, sup = _config_deux_instances({"qwen3asr": "ready", "qwen3asr-gpu0": "launched"})
    verdict = _gate(cfg, sup)
    assert verdict.action == "proceed"
    assert sup.calls == ["qwen3asr", "qwen3asr-gpu0"]


def test_multi_instance_secondaire_busy_reste_proceed():
    """Une instance secondaire indisponible dégrade le débit, pas le job."""
    cfg, sup = _config_deux_instances({"qwen3asr": "ready", "qwen3asr-gpu0": "busy"})
    verdict = _gate(cfg, sup)
    assert verdict.action == "proceed"
    assert sup.calls == ["qwen3asr", "qwen3asr-gpu0"]


def test_multi_instance_primaire_busy_defer():
    """La PREMIÈRE instance porte le verdict : busy → defer (contrat historique)."""
    cfg, sup = _config_deux_instances({"qwen3asr": "busy"})
    verdict = _gate(cfg, sup)
    assert verdict.action == "defer"
    assert sup.calls == ["qwen3asr"]  # pas d'ensure secondaire après defer

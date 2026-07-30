"""Reclaim miroir : moteurs STT servis inactifs arrêtés quand la LLM manque de VRAM.

Vécu 2026-07-19 : qwen3asr (auto-lancé par le résumé, GPU 1) + LLM 48 Go honnête
→ refus comptable à ~800 Mo près alors que le moteur avait fini de servir.

Depuis P1.a (audit 2026-07-30), la logique vit dans l'API PUBLIQUE du superviseur
(`stop_idle_engines_on`) — testée ici sur la VRAIE classe avec sonde/stoppeur/horloge
injectés — et `gpu_phase` n'est plus qu'une délégation (testée aussi)."""
from __future__ import annotations

from transcria.gpu.stt_engine_supervisor import EngineSpec, SttEngineSupervisor
from transcria.workflow.gpu_phase import GpuPhaseSession

_SPECS = [
    EngineSpec(name="qwen3asr", script="s.sh", gpu=1, gpu_mem=0.25, port=8021,
               health_url="http://127.0.0.1:8021/v1/models"),
    EngineSpec(name="autre-gpu3", script="s.sh", gpu=3, gpu_mem=0.25, port=8025,
               health_url="http://127.0.0.1:8025/v1/models"),
]


def _supervisor(*, healthy=True, stop_ok=True, now=1000.0):
    stopped: list[str] = []

    def _stopper(spec):
        stopped.append(spec.name)
        return stop_ok

    sup = SttEngineSupervisor(
        planner=object(),                       # jamais touché par stop_idle_engines_on
        health_prober=lambda url, mode="http_2xx": healthy,
        launcher=lambda spec: None,
        stopper=_stopper,
        clock=lambda: now,
    )
    return sup, stopped


class TestStopIdleEnginesOn:
    def test_arrete_le_moteur_inactif_sur_les_cartes_visees(self):
        sup, stopped = _supervisor(healthy=True)
        sup._last_used["qwen3asr"] = 940.0       # inactif depuis 60 s
        assert sup.stop_idle_engines_on({0, 1}, _SPECS) is True
        assert stopped == ["qwen3asr"]           # jamais le moteur hors placement

    def test_moteur_utilise_a_l_instant_protege(self):
        """Un job concurrent en pleine transcription (usage < 5 s) → protégé."""
        sup, stopped = _supervisor(healthy=True)
        sup._last_used["qwen3asr"] = 999.0       # utilisé il y a 1 s < min_idle_s
        assert sup.stop_idle_engines_on({0, 1}, _SPECS) is False
        assert stopped == []

    def test_moteur_eteint_rien_a_liberer(self):
        sup, stopped = _supervisor(healthy=False)
        assert sup.stop_idle_engines_on({0, 1}, _SPECS) is False
        assert stopped == []

    def test_dernier_usage_inconnu_est_arretable(self):
        """Moteur vivant jamais servi par CE process (autre worker, redémarrage) :
        arrêtable — la garde min_idle_s ne protège que l'usage CONNU récent."""
        sup, stopped = _supervisor(healthy=True)
        assert sup.stop_idle_engines_on({1}, _SPECS) is True
        assert stopped == ["qwen3asr"]


class TestDelegationGpuPhase:
    _CFG = {
        "gpu": {"llm_gpu_indices": [0, 1]},
        "resource_node": {"engines": [
            {"name": "qwen3asr", "script": "s.sh", "gpu": 1, "port": 8021},
        ]},
    }

    def test_delegue_indices_llm_et_specs_au_superviseur(self, monkeypatch):
        seen = {}

        class _Sup:
            def stop_idle_engines_on(self, indices, specs, *, min_idle_s=5.0):
                seen["indices"] = set(indices)
                seen["specs"] = [s.name for s in specs]
                return True

        monkeypatch.setattr("transcria.gpu.stt_engine_supervisor.build_stt_supervisor",
                            lambda cfg: _Sup())
        session = GpuPhaseSession(config=self._CFG, vram=object(), allocator=object())  # type: ignore[arg-type]
        assert session.reclaim_idle_stt_engines_for_llm(None) is True
        assert seen == {"indices": {0, 1}, "specs": ["qwen3asr"]}

    def test_sans_placement_declare_no_op(self):
        session = GpuPhaseSession(config={}, vram=object(), allocator=object())  # type: ignore[arg-type]
        assert session.reclaim_idle_stt_engines_for_llm(None) is False


class TestShouldReserveLlmVram:
    """P1.e (audit 2026-07-30) : une frontale À GPU avec arbitrage DISTANT réservait
    ~60 Go locaux pour une LLM qui ne tournera jamais ici — attente mensongère."""

    def _session(self, cfg):
        class _Alloc:
            @staticmethod
            def get_gpu_info():
                return [{"id": 0}]               # la machine A des GPU

        return GpuPhaseSession(config=cfg, vram=object(), allocator=_Alloc())  # type: ignore[arg-type]

    def test_arbitrage_distant_ne_reserve_jamais_localement(self):
        cfg = {"services": {"arbitrage_llm_host": "192.168.1.42"}}
        assert self._session(cfg).should_reserve_llm_vram() is False

    def test_arbitrage_local_reserve(self):
        assert self._session({}).should_reserve_llm_vram() is True

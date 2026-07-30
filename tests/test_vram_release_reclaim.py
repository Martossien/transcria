"""Les deux modules qui LIBÈRENT de la VRAM — enfin testés en direct (P2, audit 2026-07-30).

`vram_release` (registre de relâcheurs) n'était QUE monkeypatché dans la suite : le P0
(interblocage via un relâcheur réentrant) est resté invisible précisément à cause de ça.
`vram_reclaim` (arrêt de NOTRE LLM inactive) n'était couvert qu'indirectement via le
scheduler — alors que c'est la fonction qui ARRÊTE un moteur.
"""
from __future__ import annotations

import logging

from transcria.gpu import vram_release
from transcria.gpu.vram_reclaim import stop_idle_arbitrage_llm


class TestVramReleaseRegistry:
    def setup_method(self):
        self._saved = list(vram_release._releasers)
        vram_release._releasers.clear()

    def teardown_method(self):
        vram_release._releasers[:] = self._saved

    def test_appelle_chaque_relacheur(self):
        calls: list[str] = []
        vram_release.register_releaser(lambda: calls.append("a"))
        vram_release.register_releaser(lambda: calls.append("b"))
        vram_release.release_idle_vram()
        assert calls == ["a", "b"]

    def test_enregistrement_idempotent(self):
        calls: list[str] = []

        def releaser():
            calls.append("x")

        vram_release.register_releaser(releaser)
        vram_release.register_releaser(releaser)          # doublon ignoré
        vram_release.release_idle_vram()
        assert calls == ["x"]

    def test_un_relacheur_en_echec_n_arrete_pas_les_autres(self):
        calls: list[str] = []
        vram_release.register_releaser(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        vram_release.register_releaser(lambda: calls.append("survivant"))
        vram_release.release_idle_vram()                  # ne lève jamais vers l'appelant
        assert calls == ["survivant"]

    def test_relacheur_qui_s_enregistre_pendant_l_appel(self):
        """`list(_releasers)` fige l'itération : un relâcheur qui en enregistre un autre
        (chargement de façade pendant la libération) ne casse pas la boucle."""
        late: list[str] = []
        vram_release.register_releaser(
            lambda: vram_release.register_releaser(lambda: late.append("tard")))
        vram_release.release_idle_vram()
        assert late == []                                 # le nouveau servira au prochain appel
        vram_release.release_idle_vram()
        assert late == ["tard"]


class _Lock:
    """Verrou LLM factice au contrat de l'allocateur (acquire vide + release "")."""

    def __init__(self, free: bool):
        self._free = free
        self.released = False

    def try_acquire_llm(self, job_id: str = "", timeout_s: float = 0) -> bool:
        return self._free

    def release_llm(self, job_id=None) -> None:
        self.released = True


class _Vram:
    def __init__(self, running: bool):
        self._running = running
        self.stopped = False

    def is_arbitrage_llm_running(self) -> bool:
        return self._running

    def stop_arbitrage_llm(self) -> bool:
        self.stopped = True
        return True


class TestStopIdleArbitrageLlm:
    def test_arrete_quand_inactive(self):
        lock, vram = _Lock(free=True), _Vram(running=True)
        assert stop_idle_arbitrage_llm(lock, vram) is True
        assert vram.stopped is True
        assert lock.released is True                      # le verrou pris est TOUJOURS rendu

    def test_jamais_pendant_qu_un_job_l_utilise(self):
        """Le verrou occupé PROUVE qu'une phase s'en sert : on patiente, on ne tue pas."""
        lock, vram = _Lock(free=False), _Vram(running=True)
        assert stop_idle_arbitrage_llm(lock, vram) is False
        assert vram.stopped is False

    def test_rien_a_faire_si_eteinte(self):
        lock, vram = _Lock(free=True), _Vram(running=False)
        assert stop_idle_arbitrage_llm(lock, vram) is False
        assert vram.stopped is False

    def test_verrou_rendu_meme_si_l_arret_leve(self):
        lock = _Lock(free=True)
        vram = _Vram(running=True)
        vram.stop_arbitrage_llm = lambda: (_ for _ in ()).throw(RuntimeError("stop KO"))
        # Best-effort : ne lève jamais, et le verrou n'est pas confisqué.
        assert stop_idle_arbitrage_llm(lock, vram, log=logging.getLogger("t")) is False
        assert lock.released is True


class TestCheckWebGpuStatefulness:
    """P2 (audit 2026-07-30) : la règle du nœud (« workers > 1 = VRAM × N. Restons à 1 »)
    est gravée pour le tier WEB — une charge GPU à état (façade live) dans un tier
    N-workers = copies du modèle et livres aveugles entre eux."""

    def _check(self, cfg, profile, gpus):
        from transcria.diagnostics.checks.deployment import check_web_gpu_statefulness
        return check_web_gpu_statefulness(cfg, profile=profile, gpu_counter=lambda: gpus)

    def test_web_gpu_facade_active_denonce(self):
        res = self._check({"live": {"facade": {"enabled": True}}}, "web", gpus=2)
        assert res.status == "warn"
        assert "worker" in res.detail

    def test_web_gpu_sans_facade_ok(self):
        res = self._check({}, "web", gpus=2)
        assert res.status == "ok"

    def test_web_sans_gpu_ok(self):
        res = self._check({"live": {"facade": {"enabled": True}}}, "web", gpus=0)
        assert res.status == "ok"

    def test_autres_profils_sans_objet(self):
        res = self._check({"live": {"facade": {"enabled": True}}}, "all-in-one", gpus=8)
        assert res.status == "ok"

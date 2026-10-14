"""Sonde GPU unique (gpu/inventory.py — vague B3).

GPU-free : torch est substitué dans sys.modules (l'import est différé DANS
snapshot()). On vérifie le contrat qui a motivé la fusion : les deux classes
(VRAMManager, GPUAllocator) lisent le MÊME inventaire, et une panne de sonde
vaut « aucun GPU » — jamais un crash (l'ancienne copie du manager laissait
passer les RuntimeError CUDA).
"""
import sys
from types import SimpleNamespace

from transcria.gpu import inventory
from transcria.gpu.inventory import GpuState, snapshot

_GIB = 1024 ** 3


def _fake_torch(mem_by_index, *, available=True):
    names = {i: f"NVIDIA GeForce RTX 509{i}" for i in mem_by_index}
    cuda = SimpleNamespace(
        is_available=lambda: available,
        device_count=lambda: len(mem_by_index),
        mem_get_info=lambda idx: mem_by_index[idx],
        get_device_name=lambda idx: names[idx],
    )
    return SimpleNamespace(cuda=cuda)


class TestSnapshot:
    def test_reports_each_card_in_gib(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "torch", _fake_torch({
            0: (8 * _GIB, 32 * _GIB),      # (free, total)
            1: (30 * _GIB, 32 * _GIB),
        }))
        states = snapshot()
        assert [s.id for s in states] == [0, 1]
        assert states[0].free_gib == 8.0 and states[0].used_gib == 24.0 and states[0].total_gib == 32.0
        assert states[1].name == "NVIDIA GeForce RTX 5091"
        assert all(s.cuda_visible_remapped for s in states)

    def test_cuda_unavailable_is_empty(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "torch", _fake_torch({}, available=False))
        assert snapshot() == ()

    def test_probe_failure_is_empty_never_a_crash(self, monkeypatch):
        # L'ancienne copie de VRAMManager n'attrapait qu'ImportError : un RuntimeError
        # CUDA (driver en vrac, carte tombée) crashait l'admission. Politique unifiée.
        def boom(idx):
            raise RuntimeError("CUDA error: device-side assert triggered")

        fake = _fake_torch({0: (0, 0)})
        fake.cuda.mem_get_info = boom
        monkeypatch.setitem(sys.modules, "torch", fake)
        assert snapshot() == ()

    def test_as_dict_matches_historic_shape(self):
        state = GpuState(id=0, name="RTX", free_gib=8.0, used_gib=24.0, total_gib=32.0)
        assert state.as_dict() == {
            "id": 0,
            "name": "RTX",
            "cuda_visible_remapped": True,
            "memory": {"used": 24.0, "free": 8.0, "total": 32.0},
        }


class TestDelegation:
    """DoD B3 : les deux classes consomment la sonde — plus deux visions possibles."""

    _STATES = (GpuState(id=0, name="RTX", free_gib=8.0, used_gib=24.0, total_gib=32.0),)

    def test_both_classes_see_the_same_snapshot(self, monkeypatch, tmp_path):
        from builders import make_config

        from transcria.gpu.vram_manager import VRAMManager
        from transcria.queue.allocator import GPUAllocator

        monkeypatch.setattr(inventory, "snapshot", lambda: self._STATES)
        cfg = make_config(jobs_dir=tmp_path / "jobs")
        manager_view = VRAMManager(cfg).get_gpu_info()
        allocator_view = GPUAllocator(cfg).get_gpu_info()
        assert manager_view == allocator_view == [self._STATES[0].as_dict()]


class TestLectureSansContexteCuda:
    """La mesure du LIBRE passe par NVML pour ne pas ouvrir de contexte CUDA.

    `torch.cuda.mem_get_info(idx)` sélectionne la carte, donc y ouvre un contexte :
    ~500 Mio par RTX 5090 (mesuré 2026-10-12). L'inventaire sondant TOUTES les cartes,
    un process payait ~1,2 Gio de VRAM pour lire « combien est libre » — de quoi
    empêcher le lancement de la LLM d'arbitrage sur une machine chargée. NVML répond
    sans contexte ; on ne lui prend QUE le libre, le total restant celui de torch
    (base du planificateur STT). Tout doute retombe sur `mem_get_info`.
    """

    @staticmethod
    def _torch_pci(cartes, mem_get_info=None):
        """`cartes` = {index: (bus_pci, total_octets)}."""
        def _refuse(idx):
            raise AssertionError("mem_get_info appelé : un contexte CUDA aurait été ouvert")

        props = {i: SimpleNamespace(pci_domain_id=0, pci_bus_id=bus, pci_device_id=0,
                                    total_memory=total)
                 for i, (bus, total) in cartes.items()}
        cuda = SimpleNamespace(
            is_available=lambda: True,
            device_count=lambda: len(cartes),
            get_device_name=lambda idx: f"NVIDIA GeForce RTX 509{idx}",
            get_device_properties=lambda idx: props[idx],
            mem_get_info=mem_get_info or _refuse,
        )
        return SimpleNamespace(cuda=cuda)

    def test_le_libre_vient_de_nvml_et_le_total_de_torch(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "torch", self._torch_pci({0: (0x84, 32 * _GIB)}))
        vus = []
        monkeypatch.setattr(inventory, "_nvml_free_total",
                            lambda bus: vus.append(bus) or (4 * _GIB, 32 * _GIB))

        states = snapshot()

        # Apparié par bus PCI, jamais par index : torch énumère en FASTEST_FIRST et NVML
        # en ordre PCI — sur machine hétérogène les deux numérotations divergent.
        assert vus == ["00000000:84:00.0"]
        assert states[0].free_gib == 4.0
        assert states[0].total_gib == 32.0
        assert states[0].used_gib == 28.0

    def test_chaque_carte_est_lue_par_son_propre_bus(self, monkeypatch):
        # Deux cartes de MÊME nom et MÊME taille : seul le bus les distingue. Un
        # appariement par index serait silencieusement faux sous CUDA_VISIBLE_DEVICES.
        monkeypatch.setitem(sys.modules, "torch", self._torch_pci({
            0: (0x04, 32 * _GIB), 1: (0x84, 32 * _GIB),
        }))
        par_bus = {"00000000:04:00.0": (3 * _GIB, 32 * _GIB),
                   "00000000:84:00.0": (9 * _GIB, 32 * _GIB)}
        monkeypatch.setattr(inventory, "_nvml_free_total", lambda bus: par_bus[bus])

        assert [s.free_gib for s in snapshot()] == [3.0, 9.0]

    def test_mig_detecte_et_refuse_la_mesure_nvml(self, monkeypatch):
        # Sur une machine MIG, le bus mène à la carte PARENTE (80 Gio) alors que torch
        # voit une TRANCHE (10 Gio) : la mesure NVML décrirait une autre carte.
        monkeypatch.setitem(sys.modules, "torch", self._torch_pci(
            {0: (0x04, 10 * _GIB)}, mem_get_info=lambda idx: (6 * _GIB, 10 * _GIB)))
        monkeypatch.setattr(inventory, "_nvml_free_total", lambda bus: (70 * _GIB, 80 * _GIB))

        assert snapshot()[0].free_gib == 6.0   # repli, pas les 70 Gio de la carte parente

    def test_libre_borne_au_total_pilote(self, monkeypatch):
        # NVML compte le total MATÉRIEL : son libre peut dépasser le total pilote de
        # torch. Sans bornage, `used` deviendrait négatif chez les appelants.
        monkeypatch.setitem(sys.modules, "torch", self._torch_pci({0: (0x04, 32 * _GIB)}))
        monkeypatch.setattr(inventory, "_nvml_free_total",
                            lambda bus: (33 * _GIB, 33 * _GIB))

        state = snapshot()[0]
        assert state.free_gib == 32.0 and state.used_gib == 0.0

    def test_repli_sur_mem_get_info_si_nvml_muet(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "torch", self._torch_pci(
            {0: (0x04, 32 * _GIB)}, mem_get_info=lambda idx: (7 * _GIB, 32 * _GIB)))
        monkeypatch.setattr(inventory, "_nvml_free_total", lambda bus: None)

        assert snapshot()[0].free_gib == 7.0

    def test_nvml_indisponible_ne_fait_jamais_echouer_la_sonde(self, monkeypatch):
        # Machine sans pilote NVIDIA, conteneur sans libnvidia-ml : comportement
        # historique conservé.
        monkeypatch.setitem(sys.modules, "torch", self._torch_pci(
            {0: (0x04, 32 * _GIB)}, mem_get_info=lambda idx: (7 * _GIB, 32 * _GIB)))
        monkeypatch.setattr(inventory, "_nvml", lambda: None)

        assert snapshot()[0].free_gib == 7.0

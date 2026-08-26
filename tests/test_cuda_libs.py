"""Préchargement CUDA 12 pour CTranslate2 — régression du passage torch cu130.

La gate d'installation en distro vierge (2026-08-25) a attrapé la phase résumé morte
au premier chargement Whisper : torch cu130 n'embarque que les libs CUDA 13, et le
dlopen de CTranslate2 (« libcublas.so.12 ») ne relit pas LD_LIBRARY_PATH en cours de
process. Le remède est un préchargement RTLD_GLOBAL, best-effort et idempotent.
"""
from __future__ import annotations

import transcria.stt.cuda_libs as mod


def _reset():
    mod._deja_fait = False


def test_sans_paquets_nvidia_rien_ne_casse(monkeypatch):
    _reset()
    import builtins

    vrai_import = builtins.__import__

    def sans_nvidia(name, *a, **k):
        if name == "nvidia":
            raise ImportError("absent")
        return vrai_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", sans_nvidia)

    assert mod.preload_ctranslate2_cuda_libs() == 0


def test_idempotent_un_seul_passage(monkeypatch):
    _reset()
    appels = {"n": 0}

    def compte(*a, **k):
        appels["n"] += 1
        raise ImportError
    import builtins
    vrai_import = builtins.__import__
    monkeypatch.setattr(builtins, "__import__",
                        lambda name, *a, **k: compte() if name == "nvidia" else vrai_import(name, *a, **k))

    mod.preload_ctranslate2_cuda_libs()
    mod.preload_ctranslate2_cuda_libs()

    assert appels["n"] == 1  # le 2e appel sort avant même de chercher


def test_charge_les_libs_trouvees(tmp_path, monkeypatch):
    _reset()
    # un faux paquet nvidia avec une fausse lib au bon motif
    lib = tmp_path / "cublas" / "lib"
    lib.mkdir(parents=True)
    (lib / "libcublas.so.12.9.0").write_bytes(b"pas une vraie lib")

    class FauxNvidia:
        __path__ = [str(tmp_path)]
    import builtins
    vrai_import = builtins.__import__
    monkeypatch.setattr(builtins, "__import__",
                        lambda name, *a, **k: FauxNvidia if name == "nvidia" else vrai_import(name, *a, **k))

    # CDLL échouera (fichier invalide) : best-effort, 0 chargée, AUCUNE exception
    assert mod.preload_ctranslate2_cuda_libs() == 0

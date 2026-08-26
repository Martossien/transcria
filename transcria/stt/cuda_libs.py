"""Préchargement des bibliothèques CUDA 12 exigées par CTranslate2.

CTranslate2 (le moteur de faster-whisper) fait un ``dlopen("libcublas.so.12")`` — et
``dlopen`` ne relit PAS ``LD_LIBRARY_PATH`` en cours de process : modifier
l'environnement au runtime ne sert à rien. Sur un venv torch **cu130** (driver
CUDA 13, RTX 50xx), torch ne charge que les variantes CUDA 13 : le chargement de
Whisper mourait net (« Library libcublas.so.12 is not found ») — régression du
passage cu130, attrapée par la gate d'installation en distro vierge (2026-08-25).

Le remède portable : charger les `.so` cu12 des paquets pip ``nvidia-cublas-cu12`` /
``nvidia-cudnn-cu12`` en ``RTLD_GLOBAL`` AVANT l'import de faster-whisper — le
``dlopen`` suivant de CTranslate2 résout alors sur les bibliothèques déjà présentes.
Best-effort : sur un venv cu126 (où torch charge déjà les cu12), sur CPU, ou si les
paquets manquent, ne rien faire — le comportement historique est inchangé.
"""
from __future__ import annotations

import ctypes
import glob
import logging
import os

logger = logging.getLogger(__name__)

_deja_fait = False


def preload_ctranslate2_cuda_libs() -> int:
    """Charge en RTLD_GLOBAL les libs cuBLAS/cuDNN cu12 trouvées dans le venv.

    Retourne le nombre de bibliothèques chargées (0 = rien à faire ou introuvables).
    Idempotent : un seul passage par process.
    """
    global _deja_fait
    if _deja_fait:
        return 0
    _deja_fait = True
    chargees = 0
    try:
        import nvidia  # les paquets pip nvidia-* partagent ce namespace

        racines = list(getattr(nvidia, "__path__", []))
    except Exception:  # noqa: BLE001 — paquet absent : rien à précharger
        return 0
    motifs = ("cublas/lib/libcublas.so.12*", "cublas/lib/libcublasLt.so.12*",
              "cudnn/lib/libcudnn*.so.9*")
    for racine in racines:
        for motif in motifs:
            for lib in sorted(glob.glob(os.path.join(racine, motif))):
                try:
                    ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
                    chargees += 1
                except OSError as exc:
                    logger.debug("préchargement CUDA12 ignoré (%s) : %s", lib, exc)
    if chargees:
        logger.info("CTranslate2 : %d bibliothèque(s) CUDA 12 préchargée(s) "
                    "(venv torch cu130)", chargees)
    return chargees

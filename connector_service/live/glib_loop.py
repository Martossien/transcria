"""Pompage d'une boucle GLib DEPUIS asyncio — sans dépendance Python supplémentaire.

POURQUOI CE MODULE EXISTE : le Meeting SDK Zoom (bâti sur Qt/GLib) ne dispatche ses rappels
que si une boucle GLib tourne. Les exemples officiels — et les autres projets du domaine —
règlent cela en faisant de `g_main_loop_run()` la boucle PRINCIPALE, ce qui inverse le
contrôle : tout le reste (session live, façade STT, pont) devrait alors vivre dans des
rappels, ou dans un autre fil, alors que toute notre pile est asyncio.

On fait l'inverse : asyncio reste la boucle principale et POMPE GLib par petites tranches.
Vérifié en exécution réelle (conteneur `Dockerfile.zoom-sdk`) — le rappel d'authentification
arrive en 0,7 s, sur le fil qui pompe, et la boucle asyncio reste réactive pendant ce temps.

Deux conséquences utiles :
- aucune dépendance ajoutée : `ctypes` suffit, là où l'usage courant est d'installer PyGObject
  (qui exige une chaîne de compilation et des en-têtes système) ;
- les rappels du SDK arrivent sur LE FIL QUI POMPE, donc le fil asyncio : pas de transfert
  inter-fils à orchestrer pour chaque frame audio.

Les primitives GLib sont INJECTABLES : la logique de pompage est ainsi testable en CI, où
libglib n'est pas garantie présente.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

# pending() -> reste-t-il des évènements à traiter ? / iterate() -> en traiter UN, sans bloquer.
GLibPrimitives = tuple[Callable[[], bool], Callable[[], None]]


class GLibUnavailable(RuntimeError):
    """libglib introuvable ou trop ancienne — le SDK ne pourrait pas dispatcher ses rappels."""


def load_glib_primitives(library: str = "libglib-2.0.so.0") -> GLibPrimitives:
    """Charge `g_main_context_pending` / `g_main_context_iteration` sur le contexte par défaut.

    On ne charge PAS toute l'API GLib : seules ces deux fonctions sont nécessaires, et s'en
    tenir là évite d'avoir à suivre les évolutions du reste.
    """
    import ctypes

    try:
        glib = ctypes.CDLL(library)
    except OSError as exc:  # pragma: no cover — dépend de l'image
        raise GLibUnavailable(
            f"{library} introuvable : le SDK Zoom ne pourrait pas dispatcher ses rappels. "
            f"Installer le paquet système fournissant GLib."
        ) from exc

    try:
        glib.g_main_context_default.restype = ctypes.c_void_p
        glib.g_main_context_pending.argtypes = [ctypes.c_void_p]
        glib.g_main_context_pending.restype = ctypes.c_int
        glib.g_main_context_iteration.argtypes = [ctypes.c_void_p, ctypes.c_int]
        glib.g_main_context_iteration.restype = ctypes.c_int
    except AttributeError as exc:  # pragma: no cover — GLib incomplète
        raise GLibUnavailable(f"{library} n'expose pas la boucle principale attendue") from exc

    # Le contexte par défaut est résolu UNE fois : c'est celui qu'utilise le SDK, et le
    # redemander à chaque itération serait un appel inutile des milliers de fois par minute.
    context = glib.g_main_context_default()

    def pending() -> bool:
        return bool(glib.g_main_context_pending(context))

    def iterate() -> None:
        # 0 = ne PAS bloquer : bloquer ici gèlerait la boucle asyncio jusqu'au prochain
        # évènement GLib, ce qui est exactement ce qu'on cherche à éviter.
        glib.g_main_context_iteration(context, 0)

    return pending, iterate


class GLibPump:
    """Traite les évènements GLib en attente, par tranches, sans bloquer asyncio."""

    def __init__(self, primitives: GLibPrimitives | None = None, *,
                 interval_s: float = 0.005,
                 max_iterations_per_tick: int = 200) -> None:
        """`interval_s` : pause entre deux tranches. 5 ms garde la latence audio négligeable
        devant les 10-20 ms d'une frame, sans faire tourner le processeur à vide.

        `max_iterations_per_tick` : PLAFOND par tranche. Sans lui, un SDK qui produit des
        évènements en continu monopoliserait le fil et affamerait la boucle asyncio — la
        transcription s'arrêterait alors sans qu'aucune erreur ne le signale.
        """
        self._pending, self._iterate = primitives or load_glib_primitives()
        self._interval_s = interval_s
        self._max_iterations = max_iterations_per_tick
        self.iterations = 0            # exposé : utile aux gates et au diagnostic
        self.saturated_ticks = 0       # tranches ayant atteint le plafond

    def drain_once(self) -> int:
        """Traite les évènements en attente (dans la limite du plafond). Rend leur nombre."""
        processed = 0
        while processed < self._max_iterations and self._pending():
            self._iterate()
            processed += 1
        self.iterations += processed
        if processed >= self._max_iterations:
            self.saturated_ticks += 1
        return processed

    async def run(self, stop: asyncio.Event) -> None:
        """Pompe jusqu'à ce que `stop` soit armé. À lancer comme tâche asyncio parallèle."""
        try:
            while not stop.is_set():
                self.drain_once()
                await asyncio.sleep(self._interval_s)
        finally:
            # Dernière tranche : les évènements de fermeture du SDK (départ de réunion,
            # nettoyage) sont émis ici, et les perdre laisserait des ressources natives
            # pendantes.
            self.drain_once()
            if self.saturated_ticks:
                logger.warning(
                    "pompage GLib saturé sur %d tranche(s) (%d évènements) : "
                    "augmenter max_iterations_per_tick si la transcription retarde",
                    self.saturated_ticks, self.iterations)

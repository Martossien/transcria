"""Session GPU d'une phase — réservation COMPTABLE obligatoire, libération garantie.

P2 (audit 2026-07-30) : ce contexte portait DEUX protocoles distingués par duck-typing —
l'allocateur (réservation comptable) et une branche `VRAMManager.ensure_free` qui plaçait
un modèle SANS réservation (invisible des autres phases, libération `offload_all` sans
effet sur la VRAM d'un sous-process). Cette voie sans comptabilité, maintenue « pour les
tests », était le trou n°5 de l'audit : elle est fermée — l'allocateur est l'unique porte,
sur toutes les machines (sans GPU, `try_reserve` refuse et la phase bascule CPU/attente,
exactement comme avant).
"""
import logging

logger = logging.getLogger(__name__)


class GPUSession:
    """Réserve la VRAM d'une phase auprès de `GPUAllocator` (contexte `with`), et la
    libère TOUJOURS en sortie — y compris sur exception de la phase."""

    def __init__(self, allocator, model_name: str, required_mb: int,
                 job_id: str | None = None, phase: str | None = None):
        self._allocator = allocator
        self._model_name = model_name
        self._required_mb = required_mb
        self._job_id = job_id
        self._phase = phase or model_name
        self.gpu_index: int | None = None
        self.acquired: bool = False

    def __enter__(self):
        if not self._job_id:
            raise GPUSessionError(
                f"job_id requis pour réserver {self._model_name} via GPUAllocator"
            )
        reservation = self._allocator.try_reserve(
            self._job_id,
            self._required_mb,
            self._phase,
        )
        if reservation is None:
            self.acquired = False
            raise GPUSessionError(
                f"VRAM insuffisante pour {self._model_name} "
                f"({self._required_mb} Mo requis)"
            )
        self.gpu_index = reservation.gpu_index
        self.acquired = True
        logger.info(
            "GPUSession: %s alloué sur GPU %d%s",
            self._model_name,
            self.gpu_index,
            f" (job={self._job_id})" if self._job_id else "",
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.acquired:
            self._allocator.release_phase(self._job_id, self._phase)
            logger.debug(
                "GPUSession: %s libéré (GPU %d)", self._model_name, self.gpu_index
            )
        if exc_type is GPUSessionError:
            logger.warning("GPUSession: %s — %s", self._model_name, exc_val)
        return False


class GPUSessionError(Exception):
    pass

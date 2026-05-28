import logging

logger = logging.getLogger(__name__)


class GPUSession:
    def __init__(self, gpu_resource, model_name: str, required_mb: int,
                 auto_offload: bool = True, job_id: str | None = None,
                 phase: str | None = None):
        self._resource = gpu_resource
        self._model_name = model_name
        self._required_mb = required_mb
        self._auto_offload = auto_offload
        self._job_id = job_id
        self._phase = phase or model_name
        self._reservation = None
        self._uses_allocator = hasattr(gpu_resource, "try_reserve")
        self.gpu_index: int | None = None
        self.acquired: bool = False

    def __enter__(self):
        if self._uses_allocator:
            if not self._job_id:
                raise GPUSessionError(
                    f"job_id requis pour réserver {self._model_name} via GPUAllocator"
                )
            reservation = self._resource.try_reserve(
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
            self._reservation = reservation
            gpu = reservation.gpu_index
        else:
            gpu = self._resource.ensure_free(self._required_mb)
        if gpu is None:
            self.acquired = False
            raise GPUSessionError(
                f"VRAM insuffisante pour {self._model_name} "
                f"({self._required_mb} Mo requis)"
            )
        self.gpu_index = gpu
        self.acquired = True
        if not self._uses_allocator:
            self._resource.track_model(self._model_name, gpu, self._required_mb)
        logger.info(
            "GPUSession: %s alloué sur GPU %d%s",
            self._model_name,
            gpu,
            f" (job={self._job_id})" if self._job_id else "",
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.acquired:
            if self._uses_allocator:
                self._resource.release_phase(self._job_id, self._phase)
            else:
                self._resource.untrack_model(self._model_name)
                if self._auto_offload:
                    self._resource.offload_all()
            logger.debug(
                "GPUSession: %s libéré (GPU %d)", self._model_name, self.gpu_index
            )
        if exc_type is GPUSessionError:
            logger.warning("GPUSession: %s — %s", self._model_name, exc_val)
        return False


class GPUSessionError(Exception):
    pass

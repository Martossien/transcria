import logging

logger = logging.getLogger(__name__)


class GPUSession:
    def __init__(self, vram_manager, model_name: str, required_mb: int,
                 auto_offload: bool = True):
        self._vram = vram_manager
        self._model_name = model_name
        self._required_mb = required_mb
        self._auto_offload = auto_offload
        self.gpu_index: int | None = None
        self.acquired: bool = False

    def __enter__(self):
        gpu = self._vram.ensure_free(self._required_mb)
        if gpu is None:
            self.acquired = False
            raise GPUSessionError(
                f"VRAM insuffisante pour {self._model_name} "
                f"({self._required_mb} Mo requis)"
            )
        self.gpu_index = gpu
        self.acquired = True
        self._vram.track_model(self._model_name, gpu, self._required_mb)
        logger.info("GPUSession: %s alloué sur GPU %d", self._model_name, gpu)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.acquired:
            self._vram.untrack_model(self._model_name)
            if self._auto_offload:
                self._vram.offload_all()
            logger.debug(
                "GPUSession: %s libéré (GPU %d)", self._model_name, self.gpu_index
            )
        if exc_type is GPUSessionError:
            logger.warning("GPUSession: %s — %s", self._model_name, exc_val)
        return False


class GPUSessionError(Exception):
    pass

import logging

from transcria.stt.base_diarizer import BaseDiarizer

logger = logging.getLogger(__name__)

_DIARIZATION_BACKENDS = ("pyannote", "sortformer", "remote")


def create_diarizer(config: dict, device: str | None = None) -> BaseDiarizer:
    """Instancie le backend de diarisation configuré.

    Lit ``models.diarization_backend`` dans la config (défaut : ``"pyannote"``).
    Si le backend demandé est inconnu, retourne pyannote avec un warning.

    Args:
        config: Configuration complète de l'application.
        device:  Device CUDA cible (ex. ``"cuda:0"``). Si None, la valeur par
                 défaut du backend est utilisée (``"cuda:0"``).

    Returns:
        Instance concrète de BaseDiarizer.
    """
    backend = config.get("models", {}).get("diarization_backend", "pyannote")

    if backend not in _DIARIZATION_BACKENDS:
        logger.warning(
            "Backend diarisation inconnu '%s', fallback sur pyannote. "
            "Backends disponibles: %s",
            backend,
            _DIARIZATION_BACKENDS,
        )
        backend = "pyannote"

    kwargs: dict = {"config": config}
    if device is not None:
        kwargs["device"] = device

    if backend == "remote":
        from transcria.stt.remote_diarizer import RemoteDiarizer
        return RemoteDiarizer(**kwargs)

    if backend == "sortformer":
        from transcria.stt.sortformer_diarizer import SortformerDiarizer
        return SortformerDiarizer(**kwargs)

    from transcria.stt.diarization import DiarizerService
    return DiarizerService(**kwargs)


def get_diarizer_vram_mb(backend: str, config: dict) -> int:
    """Retourne la VRAM requise (Mo) pour le backend de diarisation donné.

    Args:
        backend: ``"pyannote"`` ou ``"sortformer"``.
        config:  Configuration complète de l'application.

    Returns:
        Valeur en Mo lue depuis ``config.gpu.*_vram_mb``, avec défaut intégré.
    """
    gpu_cfg = config.get("gpu", {})
    if backend == "sortformer":
        return int(gpu_cfg.get("sortformer_vram_mb", 3500))
    return int(gpu_cfg.get("pyannote_vram_mb", 2000))


def list_available_backends() -> tuple[str, ...]:
    return _DIARIZATION_BACKENDS

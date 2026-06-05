import logging
from copy import deepcopy

from transcria.stt.base_diarizer import BaseDiarizer

logger = logging.getLogger(__name__)

_DIARIZATION_BACKENDS = ("pyannote", "sortformer", "remote")

# Sortformer est un modèle à 4 locuteurs maximum ; au-delà, seul pyannote convient.
SORTFORMER_MAX_SPEAKERS = 4


def _coerce_speaker_bound(value) -> int | None:
    """Convertit une borne de locuteurs en entier >= 1, ou None si invalide."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    ival = int(value)
    return ival if ival >= 1 else None


def apply_speaker_hint(config: dict, hint: dict | None) -> dict:
    """Applique la fourchette de locuteurs choisie par l'utilisateur (par job).

    ``hint`` est un dict ``{"min": int|None, "max": int|None}`` saisi à l'upload.
    Retourne une **copie** de ``config`` avec :

    - ``diarization.min_speakers`` / ``max_speakers`` renseignés depuis le hint ;
    - ``diarization.num_speakers`` posé quand min == max (comptage exact, seul réglage
      donnant un comptage parfait sur pyannote), et retiré quand une vraie fourchette
      est fournie pour ne pas figer un ancien comptage exact ;
    - bascule de ``models.diarization_backend`` de ``sortformer`` vers ``pyannote`` quand
      la borne haute choisie par l'utilisateur dépasse la capacité de Sortformer
      (``SORTFORMER_MAX_SPEAKERS``).

    Si ``hint`` est absent ou invalide, ``config`` est renvoyé inchangé (copie).
    """
    cfg = deepcopy(config)
    if not isinstance(hint, dict):
        return cfg

    vmin = _coerce_speaker_bound(hint.get("min"))
    vmax = _coerce_speaker_bound(hint.get("max"))
    if vmin is not None and vmax is not None and vmin > vmax:
        vmin, vmax = vmax, vmin  # tolère une saisie inversée

    diar = cfg.setdefault("diarization", {})
    if vmin is not None:
        diar["min_speakers"] = vmin
    if vmax is not None:
        diar["max_speakers"] = vmax
    if vmin is not None and vmax is not None:
        if vmin == vmax:
            diar["num_speakers"] = vmin
        else:
            diar.pop("num_speakers", None)

    # Guard backend : uniquement sur la borne haute explicitement choisie par
    # l'utilisateur (jamais sur le maximum global par défaut, pour ne pas désactiver
    # Sortformer sur les configurations qui l'emploient sans fourchette saisie).
    user_upper = vmax if vmax is not None else vmin
    backend = cfg.get("models", {}).get("diarization_backend", "pyannote")
    if backend == "sortformer" and user_upper is not None and user_upper > SORTFORMER_MAX_SPEAKERS:
        cfg.setdefault("models", {})["diarization_backend"] = "pyannote"
        logger.info(
            "Diarisation: fourchette utilisateur max=%d > %d (capacité Sortformer), "
            "bascule du backend Sortformer → pyannote",
            user_upper, SORTFORMER_MAX_SPEAKERS,
        )

    return cfg


def create_diarizer(config: dict, device: str | None = None, progress_callback=None) -> BaseDiarizer:
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
    if progress_callback is not None:
        kwargs["progress_callback"] = progress_callback
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

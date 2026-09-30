import logging
from copy import deepcopy

from transcria.config.loader import default_at
from transcria.stt.base_diarizer import BaseDiarizer
from transcria.stt.diarization import DiarizerService
from transcria.stt.nemotron_diarizer import NEMOTRON_MAX_SPEAKERS, NemotronDiarizer
from transcria.stt.remote_diarizer import RemoteDiarizer
from transcria.stt.sortformer_diarizer import SortformerDiarizer

logger = logging.getLogger(__name__)

_DIARIZATION_BACKENDS = ("pyannote", "sortformer", "nemotron_diar", "remote")

# Sortformer est un modèle à 4 locuteurs maximum ; au-delà, seul pyannote convient.
SORTFORMER_MAX_SPEAKERS = 4

# Capacité des backends à nombre de voix BORNÉ par le modèle. Un backend absent de la
# table (pyannote, remote) n'a pas de plafond connu ici.
BACKEND_MAX_SPEAKERS: dict[str, int] = {
    "sortformer": SORTFORMER_MAX_SPEAKERS,
    "nemotron_diar": NEMOTRON_MAX_SPEAKERS,
}


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
    - bascule de ``models.diarization_backend`` vers ``pyannote`` quand la borne haute
      choisie par l'utilisateur dépasse la capacité du backend configuré
      (``BACKEND_MAX_SPEAKERS`` : 4 pour Sortformer, 8 pour Nemotron 3).

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
    capacity = BACKEND_MAX_SPEAKERS.get(backend)
    if capacity is not None and user_upper is not None and user_upper > capacity:
        cfg.setdefault("models", {})["diarization_backend"] = "pyannote"
        logger.info(
            "Diarisation: fourchette utilisateur max=%d > %d (capacité de %s), "
            "bascule du backend %s → pyannote",
            user_upper, capacity, backend, backend,
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
        return RemoteDiarizer(**kwargs)

    if backend == "sortformer":
        return SortformerDiarizer(**kwargs)

    if backend == "nemotron_diar":
        return NemotronDiarizer(**kwargs)

    if progress_callback is not None:
        kwargs["progress_callback"] = progress_callback
    return DiarizerService(**kwargs)


def get_diarizer_vram_mb(backend: str, config: dict) -> int:
    """Retourne la VRAM requise (Mo) pour le backend de diarisation donné.

    Args:
        backend: ``"pyannote"``, ``"sortformer"`` ou ``"nemotron_diar"``.
        config:  Configuration complète de l'application.

    Returns:
        Valeur en Mo lue depuis ``config.gpu.*_vram_mb``, avec défaut intégré.
    """
    gpu_cfg = config.get("gpu", {})
    if backend == "sortformer":
        return int(gpu_cfg.get("sortformer_vram_mb", 3500))
    if backend == "nemotron_diar":
        # Backend CPU forcé ⇒ aucune VRAM (comme Kroko : la phase saute la réservation).
        section = config.get("nemotron_diar") or {}
        if str(section.get("backend") or "auto").lower() == "cpu":
            return 0
        return int(gpu_cfg.get("nemotron_diar_vram_mb", default_at("gpu.nemotron_diar_vram_mb")))
    return int(gpu_cfg.get("pyannote_vram_mb", default_at("gpu.pyannote_vram_mb")))


def list_available_backends() -> tuple[str, ...]:
    return _DIARIZATION_BACKENDS

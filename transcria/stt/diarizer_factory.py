import logging
import os
from copy import deepcopy
from dataclasses import dataclass

from transcria.config.loader import default_at
from transcria.stt.base_diarizer import BaseDiarizer
from transcria.stt.diarization import DiarizerService
from transcria.stt.nemotron_diarizer import (
    NEMOTRON_MAX_SPEAKERS,
    NemotronDiarizer,
    cli_is_cpu_only,
    effective_cli,
    nemotron_diar_available,
)
from transcria.stt.remote_diarizer import RemoteDiarizer
from transcria.stt.sortformer_diarizer import SortformerDiarizer

logger = logging.getLogger(__name__)

# « auto » (défaut des configurations neuves depuis 0.4.7) : Nemotron 3 de 1 à 8 voix,
# pyannote au-delà ou pour un comptage exact — résolu PAR JOB, cf. resolve_auto_backend.
DIARIZATION_AUTO = "auto"
_DIARIZATION_BACKENDS = (DIARIZATION_AUTO, "pyannote", "sortformer", "nemotron_diar", "remote")

# Sortformer est un modèle à 4 locuteurs maximum ; au-delà, seul pyannote convient.
SORTFORMER_MAX_SPEAKERS = 4

# Capacité des backends à nombre de voix BORNÉ par le modèle. Un backend absent de la
# table (pyannote, remote) n'a pas de plafond connu ici.
BACKEND_MAX_SPEAKERS: dict[str, int] = {
    "sortformer": SORTFORMER_MAX_SPEAKERS,
    "nemotron_diar": NEMOTRON_MAX_SPEAKERS,
}


def configured_diarization_backend(config: dict) -> str:
    """Valeur brute de ``models.diarization_backend`` (``auto`` compris), normalisée."""
    models = config.get("models") or {}
    return str(models.get("diarization_backend", "pyannote")).strip().lower()


def _coerce_speaker_bound(value) -> int | None:
    """Convertit une borne de locuteurs en entier >= 1, ou None si invalide."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    ival = int(value)
    return ival if ival >= 1 else None


def hint_bounds(hint: dict | None) -> tuple[int | None, int | None]:
    """Bornes (min, max) d'une fourchette saisie — invalides ignorées, inversion tolérée."""
    if not isinstance(hint, dict):
        return None, None
    vmin = _coerce_speaker_bound(hint.get("min"))
    vmax = _coerce_speaker_bound(hint.get("max"))
    if vmin is not None and vmax is not None and vmin > vmax:
        vmin, vmax = vmax, vmin
    return vmin, vmax


@dataclass(frozen=True)
class AutoResolution:
    """Backend retenu pour « auto » et la raison (journal, doctor, tests)."""

    backend: str
    reason: str


def resolve_auto_backend(
    *,
    upper_bound: int | None,
    exact_count: bool,
    nemotron_available: bool,
    pyannote_usable: bool,
) -> AutoResolution:
    """La règle « auto », PURE et sans E/S.

    - Nemotron 3 absent (binaire ou GGUF) → pyannote : le comportement historique ;
    - pyannote inutilisable (aucun token HF) → Nemotron 3 quoi qu'il arrive — mieux
      qu'un échec, même au-delà de 8 voix (l'appelant journalise l'avertissement) ;
    - comptage EXACT saisi (min == max) → pyannote, le seul backend qui honore
      ``num_speakers`` (notre seul comptage parfait mesuré) ;
    - borne haute saisie > 8 → pyannote (capacité du modèle) ;
    - sinon (fourchette absente ou ≤ 8) → Nemotron 3 : sans token, en quelques secondes.
    """
    if not nemotron_available:
        return AutoResolution("pyannote", "nemotron_unavailable")
    if not pyannote_usable:
        return AutoResolution("nemotron_diar", "pyannote_unusable")
    if exact_count:
        return AutoResolution("pyannote", "exact_count")
    if upper_bound is not None and upper_bound > NEMOTRON_MAX_SPEAKERS:
        return AutoResolution("pyannote", "above_capacity")
    return AutoResolution("nemotron_diar", "default")


def pyannote_usable() -> bool:
    """pyannote est *gated* : sans token HF dans l'environnement, il ne se charge pas."""
    return bool(os.environ.get("HF_TOKEN"))


def resolve_diarization_backend(
    config: dict,
    hint: dict | None = None,
    *,
    nemotron_available: bool | None = None,
    pyannote_ok: bool | None = None,
) -> AutoResolution:
    """Backend EFFECTIF pour ce job : la valeur configurée, ou la résolution de « auto ».

    Les deux sondes sont injectables (tests, doctor) ; par défaut elles lisent le disque
    (binaire + GGUF) et l'environnement (token HF) — jamais le réseau.
    """
    configured = configured_diarization_backend(config)
    if configured != DIARIZATION_AUTO:
        return AutoResolution(configured, "configured")
    vmin, vmax = hint_bounds(hint)
    upper = vmax if vmax is not None else vmin
    exact = vmin is not None and vmax is not None and vmin == vmax
    return resolve_auto_backend(
        upper_bound=upper,
        exact_count=exact,
        nemotron_available=nemotron_diar_available(config) if nemotron_available is None else nemotron_available,
        pyannote_usable=pyannote_usable() if pyannote_ok is None else pyannote_ok,
    )


def apply_speaker_hint(
    config: dict,
    hint: dict | None,
    *,
    nemotron_available: bool | None = None,
    pyannote_ok: bool | None = None,
) -> dict:
    """Applique la fourchette de locuteurs choisie par l'utilisateur (par job).

    ``hint`` est un dict ``{"min": int|None, "max": int|None}`` saisi à l'upload.
    Retourne une **copie** de ``config`` avec :

    - ``diarization.min_speakers`` / ``max_speakers`` renseignés depuis le hint ;
    - ``diarization.num_speakers`` posé quand min == max (comptage exact, seul réglage
      donnant un comptage parfait sur pyannote), et retiré quand une vraie fourchette
      est fournie pour ne pas figer un ancien comptage exact ;
    - ``models.diarization_backend`` « auto » RÉSOLU pour ce job (``resolve_auto_backend`` :
      Nemotron 3 jusqu'à 8 voix, pyannote au-delà ou pour un comptage exact) — toujours,
      même sans fourchette saisie, pour que l'appelant lise un backend concret ;
    - bascule de ``models.diarization_backend`` vers ``pyannote`` quand la borne haute
      choisie par l'utilisateur dépasse la capacité du backend configuré
      (``BACKEND_MAX_SPEAKERS`` : 4 pour Sortformer, 8 pour Nemotron 3).

    Si ``hint`` est absent ou invalide, ``config`` est renvoyé inchangé (copie), à la
    résolution de « auto » près.
    """
    cfg = deepcopy(config)
    configured = configured_diarization_backend(cfg)
    if configured == DIARIZATION_AUTO:
        resolution = resolve_diarization_backend(
            cfg, hint, nemotron_available=nemotron_available, pyannote_ok=pyannote_ok)
        cfg.setdefault("models", {})["diarization_backend"] = resolution.backend
        cfg["models"]["diarization_backend_auto_reason"] = resolution.reason
        logger.info("Diarisation « auto » → %s (%s)", resolution.backend, resolution.reason)
        vmin_log, vmax_log = hint_bounds(hint)
        if resolution.reason == "pyannote_unusable" and (vmax_log or vmin_log or 0) > NEMOTRON_MAX_SPEAKERS:
            logger.warning(
                "Diarisation « auto » : fourchette > %d voix mais pyannote inutilisable (aucun token HF) — "
                "Nemotron 3 retenu, les voix au-delà de sa capacité seront fusionnées",
                NEMOTRON_MAX_SPEAKERS,
            )
    if not isinstance(hint, dict):
        return cfg

    vmin, vmax = hint_bounds(hint)

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
    backend = configured_diarization_backend(cfg)
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
    backend = configured_diarization_backend(config)
    if backend == DIARIZATION_AUTO:
        # Appelant sans passage par apply_speaker_hint (détection sans fourchette) :
        # même règle, sans fourchette.
        backend = resolve_diarization_backend(config).backend

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


def get_diarizer_vram_mb(backend: str, config: dict, *, at_upload: bool = False) -> int:
    """Retourne la VRAM requise (Mo) pour le backend de diarisation donné.

    Args:
        backend: ``"pyannote"``, ``"sortformer"``, ``"nemotron_diar"`` ou ``"auto"`` (résolu
                 sans fourchette : l'admission est estimée AVANT la lecture du job).
        config:  Configuration complète de l'application.
        at_upload: la passe de détection lancée dès l'upload (résumé). Nemotron 3 en
                 ``nemotron_diar.backend: auto`` y tourne sur CPU (0 Mo) : rien n'attend
                 la carte, la LLM n'est jamais préemptée, et le pipeline réutilise le
                 checkpoint. ``cuda`` explicite garde le GPU partout.

    Returns:
        Valeur en Mo lue depuis ``config.gpu.*_vram_mb``, avec défaut intégré.
    """
    gpu_cfg = config.get("gpu", {})
    if backend == DIARIZATION_AUTO:
        backend = resolve_diarization_backend(config).backend
    if backend == "sortformer":
        return int(gpu_cfg.get("sortformer_vram_mb", 3500))
    if backend == "nemotron_diar":
        # Backend CPU forcé ⇒ aucune VRAM (comme Kroko : la phase saute la réservation).
        section = config.get("nemotron_diar") or {}
        mode = str(section.get("backend") or "auto").lower()
        if mode == "cpu" or (mode == "auto" and at_upload):
            return 0
        if cli_is_cpu_only(effective_cli(config)):
            # Précompilé CPU seul : réserver un GPU n'aurait aucun sens (et le binaire
            # refuse `--backend cuda`) — vécu à la gate d'installation 2026-10-10.
            return 0
        return int(gpu_cfg.get("nemotron_diar_vram_mb", default_at("gpu.nemotron_diar_vram_mb")))
    return int(gpu_cfg.get("pyannote_vram_mb", default_at("gpu.pyannote_vram_mb")))


def list_available_backends() -> tuple[str, ...]:
    return _DIARIZATION_BACKENDS

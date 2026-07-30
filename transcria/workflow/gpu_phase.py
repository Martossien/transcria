"""Session GPU des phases du workflow (vague B1, étape 1).

Regroupe les 8 méthodes « session GPU » extraites de ``WorkflowRunner`` : réservation/
libération VRAM d'une phase, détection des phases servies à distance (0 VRAM locale),
récupération de VRAM sur la LLM d'arbitrage inactive. C'est ici que meurt le couplage
runner→infrastructure : ``VRAMManager`` et ``GPUAllocator`` sont **reçus en paramètres**
(défauts = constructions historiques — pas de framework DI, des factories explicites).

Les classes GPU elles-mêmes (``vram_manager``, ``gpu_allocator``) sont gelées pendant
les vagues A/B0/B1/B2 (plan §9) : ce module les enveloppe sans les modifier.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

from transcria.gpu.arbitrage_endpoint import is_remote_arbitrage
from transcria.gpu.gpu_session import GPUSession
from transcria.gpu.vram_manager import VRAMManager
from transcria.gpu.vram_reclaim import stop_idle_arbitrage_llm
from transcria.jobs.models import Job
from transcria.queue.allocator import GPUAllocator
from transcria.stt.transcriber_factory import _should_use_remote_stt, summary_backend

logger = logging.getLogger(__name__)


class _NoReservationSession:
    """Session GPU no-op : phase servie à distance OU backend CPU pur (0 Mo VRAM).

    Expose `gpu_index` (device de repli/fallback éventuel ; None = CPU) sans rien
    réserver ni décharger — la VRAM est ailleurs (serveur distant) ou inutile.
    """

    def __init__(self, gpu_index: int | None) -> None:
        self.gpu_index = gpu_index
        self.acquired = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class GpuPhaseSession:
    """Réservations GPU/VRAM au service des phases — l'unique porte du workflow vers
    l'infrastructure GPU."""

    def __init__(
        self,
        config: dict | None = None,
        vram: VRAMManager | None = None,
        allocator: GPUAllocator | None = None,
    ) -> None:
        self.config = config or {}
        self.vram = vram if vram is not None else VRAMManager(config=self.config)
        self.allocator = allocator if allocator is not None else GPUAllocator.get_instance(self.config)

    def session(self, job: Job, model_name: str, required_mb: int, phase: str):
        if self.phase_runs_remotely(phase):
            logger.info("Phase %s servie à distance — session GPU sans réservation locale", phase)
            return _NoReservationSession(self.default_remote_gpu_index())
        if required_mb <= 0:
            # Backend CPU pur (ex. kroko) : rien à réserver (marcherait aussi sans GPU).
            logger.info("Phase %s sur CPU (0 Mo VRAM) — session GPU sans réservation", phase)
            return _NoReservationSession(None)
        # P2 (audit 2026-07-30) : l'allocateur est l'UNIQUE porte — la branche sans
        # comptabilité (GPUSession sur VRAMManager, compat « fakes historiques ») plaçait
        # des modèles invisibles des autres phases. Machine sans GPU : try_reserve refuse
        # et la phase bascule CPU/attente, comme avant.
        return GPUSession(
            self.allocator,
            model_name,
            required_mb,
            job_id=job.id,
            phase=phase,
        )

    def reserve_phase(self, job: Job, required_mb: int, phase: str):
        if self.phase_runs_remotely(phase):
            logger.info("Phase %s servie à distance — aucune réservation VRAM locale", phase)
            return SimpleNamespace(gpu_index=self.default_remote_gpu_index()), False
        if required_mb <= 0:
            # Backend CPU pur (ex. kroko) : aucune VRAM requise ⇒ aucune réservation,
            # sinon on bloquerait un slot GPU (ou la machine sans GPU) pour rien.
            logger.info("Phase %s sur CPU (0 Mo VRAM) — aucune réservation GPU", phase)
            return SimpleNamespace(gpu_index=None), False
        reservation = self.allocator.try_reserve(job.id, required_mb, phase)
        if reservation is not None:
            return reservation, True
        # P2 (audit 2026-07-30) : plus de repli `ensure_free` sans comptabilité — un refus
        # de l'allocateur signifie « pas de place », la phase attend/bascule (vram_wait).
        return None, False

    def release_phase(self, job: Job, phase: str, managed_by_allocator: bool) -> None:
        if managed_by_allocator:
            self.allocator.release_phase(job.id, phase)
        # Sinon : session sans réservation (distante/CPU) — rien à libérer. L'ancien
        # `offload_all()` vidait un cache CUDA dans le MAUVAIS process (geste sans effet,
        # entretenant l'illusion d'une libération — audit 2026-07-30).

    def should_reserve_llm_vram(self) -> bool:
        """Faut-il réserver la VRAM LLM LOCALEMENT ? Non sans GPU, et non si l'arbitrage
        est DISTANT (P1.e, audit 2026-07-30) : sur une frontale À GPU (STT local + LLM
        sur un nœud), réserver ~60 Go locaux pour une LLM qui ne tournera jamais ici
        produisait, dès que la distante saturait, une attente « VRAM insuffisante »
        mensongère sur une machine incapable de l'héberger."""
        if is_remote_arbitrage(self.config or {}):
            return False
        return bool(self.allocator.get_gpu_info())

    def phase_runs_remotely(self, phase: str) -> bool:
        """True si la capacité de cette phase est servie à distance → 0 VRAM locale.

        Évite la réservation fantôme observée en mode distant (un run 100 % distant
        réservait quand même `phase=stt vram=6000` localement, d'où fausse contention
        VRAM / rejets à tort). Cf. docs/SERVICE_RESSOURCES_GPU.md §9.
        """
        if phase in ("stt", "summary_stt"):
            # La phase résumé peut avoir son propre backend (models.summary_stt_backend,
            # ex. kroko local) : sa « distance » se juge sur CE backend, pas le principal.
            backend = (summary_backend(self.config) if phase == "summary_stt"
                       else self.config.get("models", {}).get("stt_backend", "cohere"))
            return _should_use_remote_stt(self.config, backend)
        if phase == "diarization":
            return self.config.get("models", {}).get("diarization_backend") == "remote"
        return False

    def default_remote_gpu_index(self) -> int:
        """Index GPU « device » fourni aux adaptateurs distants (utilisé seulement
        pour un éventuel fallback local ; aucune VRAM n'est réservée)."""
        pg = getattr(self.allocator, "preferred_gpu", None)
        return int(pg) if pg is not None else 0

    @staticmethod
    def cuda_available() -> bool:
        try:
            import torch  # différé : dépendance lourde de boot, sondée à la demande
            return bool(torch.cuda.is_available())
        except Exception:
            return False

    def reclaim_idle_stt_engines_for_llm(self, sl, *, min_idle_s: float = 5.0) -> bool:
        """Libère la VRAM des moteurs STT SERVIS locaux inactifs sur les GPU du
        placement LLM — MIROIR de `reclaim_idle_arbitrage_llm` (vécu 2026-07-19 :
        sur machine serrée, le moteur qwen3asr auto-lancé par le résumé occupait
        le GPU 1 quand la LLM 48 Go voulait s'y placer → refus comptable à ~800 Mo
        près, alors que le moteur avait FINI de servir et se relance à la demande).

        Prudence : uniquement les moteurs du manifeste local et vivants. Le garde
        `min_idle_s` (dernier usage connu) est PAR INSTANCE de superviseur — dans le
        process workflow il est souvent vide, donc best-effort : la vraie sécurité
        pour un job concurrent est en aval (retries de l'AsrClient + bascule du pool
        multi-instance + relance à la demande par le pré-vol, cycle A/B/C). Retourne
        True si au moins un moteur a été arrêté (l'appelant retente UNE fois)."""
        from transcria.gpu.stt_engine_supervisor import (
            build_stt_supervisor,
            engine_specs_from_config,
        )

        config = self.config or {}
        llm_indices = set((config.get("gpu", {}) or {}).get("llm_gpu_indices") or [])
        if not llm_indices:
            return False
        try:
            supervisor = build_stt_supervisor(config)
        except Exception:  # noqa: BLE001 — jamais bloquant pour une phase LLM
            return False
        # Délégation à l'API PUBLIQUE du superviseur (P1.a) : l'état d'usage des moteurs
        # lui appartient — cette façade ne pioche plus dans ses membres privés.
        return supervisor.stop_idle_engines_on(
            llm_indices, engine_specs_from_config(config), min_idle_s=min_idle_s)

    def reclaim_idle_arbitrage_llm(self, sl) -> bool:
        """Libère la VRAM en arrêtant NOTRE LLM d'arbitrage inactive (catégorie 1).

        Délègue au helper partagé `stop_idle_arbitrage_llm` (mutualisé avec l'admission
        du scheduler). N'arrête la LLM que si elle tourne et que le verrou LLM est libre
        (aucun job ne l'utilise). Jamais un process tiers.
        """
        return stop_idle_arbitrage_llm(self.allocator, self.vram, log=sl)

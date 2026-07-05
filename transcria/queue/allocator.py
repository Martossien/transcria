from __future__ import annotations

import gc
import json
import logging
import os
import signal
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from transcria.gpu.cuda_visible import (
    parse_cuda_visible_devices,
    to_nvidia_smi_gpu_index,
    to_visible_device_index,
)
from transcria.gpu.opencode_setup import is_remote_arbitrage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Reservation:
    job_id: str
    gpu_index: int
    vram_mb: int
    phase: str
    reserved_at: float


class GPUAllocator:
    """Allocateur GPU centralisé et thread-safe.

    La réservation est volontairement comptable : elle coordonne les workers
    TranscrIA entre eux. La VRAM réellement libre reste l'autorité finale via
    `get_gpu_info()`.
    """

    _instance: GPUAllocator | None = None
    _instance_lock = threading.Lock()

    def __init__(self, config: dict):
        self.config = config
        gpu_cfg = config.get("gpu", {}) or {}
        scheduling_cfg = config.get("workflow", {}).get("scheduling", {}) or {}

        self.min_free_mb = int(gpu_cfg.get("min_free_vram_mb", 4000))
        self.preferred_gpu = self._resolve_preferred_gpu()
        self._kill_patterns = [
            str(item).lower()
            for item in scheduling_cfg.get(
                "kill_patterns",
                [
                    "vllm",
                    "llama-server",
                    "text-generation-server",
                    "aphrodite",
                    "sglang",
                    "lmdeploy",
                    "exllamav2",
                ],
            )
            if str(item).strip()
        ]

        default_pid_file = Path(
            config.get("storage", {}).get("jobs_dir", ".")
        ) / ".transcria_pids"
        pid_file = scheduling_cfg.get("pid_file") or str(default_pid_file)
        self._pid_file = Path(pid_file)
        if not self._pid_file.is_absolute():
            self._pid_file = Path.cwd() / self._pid_file

        self._gpu_reservations: dict[int, list[Reservation]] = {}
        self._alloc_lock = threading.RLock()
        self._llm_lock = threading.Lock()
        self._llm_owner: str | None = None
        self._llm_owner_lock = threading.Lock()
        # LLM d'arbitrage DISTANTE (vLLM sur un nœud) : elle batche les requêtes concurrentes —
        # le verrou LLM local (hérité du modèle « une LLM locale mono-GPU ») la sérialiserait à
        # tort et étranglerait le débit. On le neutralise alors (acquire/release no-op). En LOCAL
        # le verrou reste actif (coordination VRAM/préemption). Cf. opencode_setup.is_remote_arbitrage.
        self._arbitrage_remote = is_remote_arbitrage(config)
        self._tracked_pids: dict[int, str] = {}
        self._pid_lock = threading.Lock()
        self.reload_pids()

    @classmethod
    def get_instance(cls, config: dict | None = None) -> GPUAllocator:
        with cls._instance_lock:
            if cls._instance is None:
                if config is None:
                    raise ValueError("config requise à la première initialisation GPUAllocator")
                cls._instance = cls(config)
            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Réinitialise le singleton. Réservé aux tests et au redémarrage contrôlé."""
        with cls._instance_lock:
            cls._instance = None

    @staticmethod
    def _resolve_preferred_gpu() -> int:
        env_gpu = os.environ.get("TRANSCRIA_PREFERRED_GPU")
        if not env_gpu:
            return 0
        try:
            return int(env_gpu)
        except ValueError:
            logger.warning("TRANSCRIA_PREFERRED_GPU invalide: %s", env_gpu)
            return 0

    def get_gpu_info(self) -> list[dict]:
        # Source LOCALE (C2.3) : détour dashboard externe retiré (repli = vraie source).
        return self._get_gpu_info_local()

    def _get_gpu_info_local(self) -> list[dict]:
        gpus: list[dict] = []
        try:
            import torch

            if torch.cuda.is_available():
                for idx in range(torch.cuda.device_count()):
                    free, total = torch.cuda.mem_get_info(idx)
                    gpus.append(
                        {
                            "id": idx,
                            "name": torch.cuda.get_device_name(idx),
                            "cuda_visible_remapped": True,
                            "memory": {
                                "used": (total - free) / (1024**3),
                                "free": free / (1024**3),
                                "total": total / (1024**3),
                            },
                        }
                    )
        except Exception:
            pass
        return gpus

    @staticmethod
    def _visible_cuda_device_count() -> int | None:
        visible = parse_cuda_visible_devices()
        if visible is None:
            return None
        return len(visible)

    def _reserved_vram_mb_locked(self, gpu_index: int, exclude_job_phase: tuple[str, str] | None = None) -> int:
        total = 0
        for reservation in self._gpu_reservations.get(gpu_index, []):
            if exclude_job_phase and (
                reservation.job_id,
                reservation.phase,
            ) == exclude_job_phase:
                continue
            total += reservation.vram_mb
        return total

    def get_available_vram_mb(self, gpu_index: int) -> int:
        with self._alloc_lock:
            return self._get_available_vram_mb_locked(gpu_index)

    def _get_available_vram_mb_locked(self, gpu_index: int) -> int:
        real_free = 0
        visible_devices = parse_cuda_visible_devices()
        for gpu in self.get_gpu_info():
            if to_visible_device_index(
                gpu.get("id", 0),
                visible_devices,
                allow_remapped_ordinal=bool(gpu.get("cuda_visible_remapped")),
            ) == gpu_index:
                real_free = int(float(gpu.get("memory", {}).get("free", 0)) * 1024)
                break
        return max(0, real_free - self._reserved_vram_mb_locked(gpu_index))

    def try_reserve(
        self,
        job_id: str,
        required_mb: int,
        phase: str,
        preferred_gpu: int | None = None,
    ) -> Reservation | None:
        """Réserve atomiquement une phase GPU pour un job."""
        required_mb = int(required_mb)
        if required_mb <= 0:
            raise ValueError("required_mb doit être positif")
        if not job_id:
            raise ValueError("job_id requis")
        if not phase:
            raise ValueError("phase requise")

        with self._alloc_lock:
            existing = self._find_reservation_locked(job_id, phase)
            if existing is not None:
                return existing

            gpu_index = self._select_gpu_locked(required_mb, preferred_gpu)
            if gpu_index is None:
                logger.info(
                    "Allocation GPU impossible: job=%s phase=%s besoin=%d Mo",
                    job_id,
                    phase,
                    required_mb,
                )
                return None

            reservation = Reservation(
                job_id=job_id,
                gpu_index=gpu_index,
                vram_mb=required_mb,
                phase=phase,
                reserved_at=time.monotonic(),
            )
            self._gpu_reservations.setdefault(gpu_index, []).append(reservation)
            logger.info(
                "GPU réservé: job=%s phase=%s gpu=%d vram=%d Mo",
                job_id,
                phase,
                gpu_index,
                required_mb,
            )
            return reservation

    def _find_reservation_locked(self, job_id: str, phase: str) -> Reservation | None:
        for reservations in self._gpu_reservations.values():
            for reservation in reservations:
                if reservation.job_id == job_id and reservation.phase == phase:
                    return reservation
        return None

    def _select_gpu_locked(self, required_mb: int, preferred_gpu: int | None) -> int | None:
        visible_devices = parse_cuda_visible_devices()
        candidates = self.get_gpu_info()
        if preferred_gpu is None:
            preferred_gpu = self.preferred_gpu

        ordered: list[dict] = []
        for gpu in candidates:
            visible_gpu = to_visible_device_index(
                gpu.get("id", 0),
                visible_devices,
                allow_remapped_ordinal=bool(gpu.get("cuda_visible_remapped")),
            )
            if visible_gpu == preferred_gpu:
                ordered.insert(0, gpu)
            else:
                ordered.append(gpu)

        # Préserver les cartes du placement LLM : une petite phase (STT 6 Go, pyannote
        # 2 Go) posée sur une carte de la LLM bloquerait sa (re)mise en route alors que
        # d'autres cartes conviennent. On préfère donc les GPU HORS placement, à
        # disponibilité suffisante (avec le défaut « tous les GPU », aucun effet).
        try:
            llm_gpus = set(self._llm_gpu_indices())
        except Exception:  # noqa: BLE001 — la sélection ne doit jamais échouer pour ça
            llm_gpus = set()

        best_idx: int | None = None
        best_key: tuple[bool, int] | None = None
        for gpu in ordered:
            gpu_id = to_visible_device_index(
                gpu.get("id", 0),
                visible_devices,
                allow_remapped_ordinal=bool(gpu.get("cuda_visible_remapped")),
            )
            if gpu_id is None:
                continue
            available = self._get_available_vram_mb_locked(gpu_id)
            if available < required_mb + self.min_free_mb:
                continue
            key = (gpu_id not in llm_gpus, available)
            if best_key is None or key > best_key:
                best_idx = gpu_id
                best_key = key
        return best_idx

    def can_allocate(self, required_mb: int, preferred_gpu: int | None = None) -> int | None:
        """Compatibilité lecture seule. Utiliser `try_reserve()` pour lancer une phase."""
        with self._alloc_lock:
            return self._select_gpu_locked(int(required_mb), preferred_gpu)

    # ── LLM d'arbitrage : moteur MULTI-GPU à placement contrôlé par le script ──────
    #
    # La LLM (ex. 35B Q8 ≈ 60 Go) s'étale sur plusieurs cartes via le script de
    # lancement (CUDA_VISIBLE_DEVICES + --tensor-split) : son besoin ne tiendra JAMAIS
    # sur un seul GPU. La modéliser par `try_reserve()` mono-GPU était insatisfaisable
    # par construction (audit du 11/06/2026) : on la modélise désormais comme un besoin
    # PAR GPU (total ÷ nb de cartes) vérifié/réservé sur les GPU que le script utilise
    # réellement (`gpu.llm_gpu_indices`, défaut = tous les GPU visibles).

    def _llm_gpu_indices(self) -> list[int]:
        """GPU (index visibles) utilisés par le script LLM. Défaut : tous."""
        configured = (self.config.get("gpu", {}) or {}).get("llm_gpu_indices")
        if isinstance(configured, list) and configured:
            return [int(i) for i in configured]
        visible_devices = parse_cuda_visible_devices()
        indices = []
        for gpu in self.get_gpu_info():
            idx = to_visible_device_index(
                gpu.get("id", 0), visible_devices,
                allow_remapped_ordinal=bool(gpu.get("cuda_visible_remapped")),
            )
            if idx is not None:
                indices.append(idx)
        return sorted(indices)

    @staticmethod
    def _llm_per_gpu_mb(total_mb: int, gpu_count: int) -> int:
        return -(-int(total_mb) // max(1, gpu_count))  # plafond (ceil)

    def _llm_shares(self, total_mb: int, indices: list[int]) -> dict[int, int]:
        """Part de VRAM par GPU du placement LLM.

        Cartes HÉTÉROGÈNES (8/12/16/24/48 Go…) ou `--tensor-split` inégal :
        `gpu.llm_vram_mb_per_gpu` (liste alignée sur `llm_gpu_indices`) déclare la part
        réelle de chaque carte. À défaut : répartition égale (split homogène)."""
        per_gpu = (self.config.get("gpu", {}) or {}).get("llm_vram_mb_per_gpu")
        if isinstance(per_gpu, list) and len(per_gpu) == len(indices) and all(
            isinstance(mb, (int, float)) and mb > 0 for mb in per_gpu
        ):
            return {idx: int(mb) for idx, mb in zip(indices, per_gpu)}
        share = self._llm_per_gpu_mb(total_mb, len(indices))
        return {idx: share for idx in indices}

    def can_host_llm(self, total_mb: int) -> bool:
        """Chaque GPU du placement LLM a-t-il SA part requise (+ marge) de libre ?"""
        indices = self._llm_gpu_indices()
        if not indices:
            return False
        shares = self._llm_shares(total_mb, indices)
        with self._alloc_lock:
            return all(
                self._get_available_vram_mb_locked(idx) >= shares[idx] + self.min_free_mb
                for idx in indices
            )

    def try_reserve_llm(self, job_id: str, total_mb: int, phase: str) -> bool:
        """Réserve la LLM multi-GPU pour un job : une part par GPU du placement,
        TOUT-OU-RIEN (aucune réservation laissée en cas d'échec partiel).

        Idempotent par (job, phase) ; libérer via `release_phase(job_id, phase)`
        (qui supprime déjà toutes les parts) ou `release(job_id)`."""
        if not job_id or not phase:
            raise ValueError("job_id et phase requis")
        total_mb = int(total_mb)
        if total_mb <= 0:
            raise ValueError("total_mb doit être positif")

        indices = self._llm_gpu_indices()
        if not indices:
            return False
        shares = self._llm_shares(total_mb, indices)
        with self._alloc_lock:
            if self._find_reservation_locked(job_id, phase) is not None:
                return True
            for idx in indices:
                if self._get_available_vram_mb_locked(idx) < shares[idx] + self.min_free_mb:
                    logger.info(
                        "Allocation LLM impossible: job=%s phase=%s besoin=%d Mo (parts=%s, GPU %d insuffisant)",
                        job_id, phase, total_mb, shares, idx,
                    )
                    return False
            now = time.monotonic()
            for idx in indices:
                self._gpu_reservations.setdefault(idx, []).append(Reservation(
                    job_id=job_id, gpu_index=idx, vram_mb=shares[idx], phase=phase, reserved_at=now,
                ))
            logger.info(
                "LLM réservée: job=%s phase=%s total=%d Mo (parts=%s)",
                job_id, phase, total_mb, shares,
            )
            return True

    def reserve(self, job_id: str, gpu_index: int, vram_mb: int, phase: str = "stt") -> bool:
        with self._alloc_lock:
            existing = self._find_reservation_locked(job_id, phase)
            if existing is not None:
                return existing.gpu_index == gpu_index
            available = self._get_available_vram_mb_locked(gpu_index)
            if available < int(vram_mb) + self.min_free_mb:
                return False
            self._gpu_reservations.setdefault(gpu_index, []).append(
                Reservation(job_id, gpu_index, int(vram_mb), phase, time.monotonic())
            )
            return True

    def release_phase(self, job_id: str, phase: str) -> None:
        released = 0
        with self._alloc_lock:
            for gpu_index, reservations in list(self._gpu_reservations.items()):
                kept = []
                for reservation in reservations:
                    if reservation.job_id == job_id and reservation.phase == phase:
                        released += reservation.vram_mb
                    else:
                        kept.append(reservation)
                if kept:
                    self._gpu_reservations[gpu_index] = kept
                else:
                    self._gpu_reservations.pop(gpu_index, None)
        if released:
            self._cleanup_cuda_cache()
            logger.info("GPU libéré: job=%s phase=%s vram=%d Mo", job_id, phase, released)

    def release_reservations(self, job_id: str) -> int:
        """Libère les réservations VRAM (accounting) d'un job — **sans** toucher au verrou
        LLM (`threading.Lock` partagé, dont la libération hors propriétaire est délicate).

        Ne supprime QUE les réservations de CE job (sûr même avec d'autres jobs
        concurrents) ; idempotent (no-op si rien à libérer). Retourne les Mo récupérés.
        Sert de **filet de sécurité** de fin de job (cf. `QueueScheduler._on_done`) : les
        phases libèrent déjà via `GPUSession`/`finally`, ceci garantit zéro fuite
        d'accounting même sur un chemin de crash imprévu.
        """
        released = 0
        with self._alloc_lock:
            for gpu_index, reservations in list(self._gpu_reservations.items()):
                kept = []
                for reservation in reservations:
                    if reservation.job_id == job_id:
                        released += reservation.vram_mb
                    else:
                        kept.append(reservation)
                if kept:
                    self._gpu_reservations[gpu_index] = kept
                else:
                    self._gpu_reservations.pop(gpu_index, None)
        if released:
            self._cleanup_cuda_cache()
        return released

    def release(self, job_id: str) -> None:
        released = self.release_reservations(job_id)
        self.release_llm(job_id)
        if released:
            logger.info("Réservations GPU libérées: job=%s vram=%d Mo", job_id, released)

    @staticmethod
    def _cleanup_cuda_cache() -> None:
        gc.collect()
        try:
            import torch

            torch.cuda.empty_cache()
        except Exception:
            pass

    def try_acquire_llm(self, job_id: str = "", timeout_s: float = 0) -> bool:
        # LLM distante (vLLM batché) : aucune sérialisation locale — toujours « acquis ».
        if self._arbitrage_remote:
            return True
        acquired = self._llm_lock.acquire(timeout=timeout_s) if timeout_s else self._llm_lock.acquire(blocking=False)
        if acquired:
            with self._llm_owner_lock:
                self._llm_owner = job_id or None
            logger.info("Verrou LLM acquis", extra={"job_id": job_id})
        return acquired

    def release_llm(self, job_id: str | None = None) -> None:
        if self._arbitrage_remote:
            return
        # Propriété STRICTE : un release ciblé (job_id fourni) ne libère le verrou QUE si
        # ce job en est bien le propriétaire courant. Sinon (owner différent OU owner=None
        # — fenêtre entre `lock.acquire()` d'un autre job et la pose de son owner), c'est
        # un release périmé : ne rien faire, sous peine de voler le verrou du détenteur.
        # Le check ET le release restent dans la SECTION CRITIQUE owner_lock (atomiques).
        # Un release sans job_id (force/admin) reste inconditionnel.
        with self._llm_owner_lock:
            if job_id and self._llm_owner != job_id:
                return
            self._llm_owner = None
            try:
                self._llm_lock.release()
                logger.info("Verrou LLM libéré", extra={"job_id": job_id or ""})
            except RuntimeError:
                pass

    def register_pid(self, pid: int, label: str) -> None:
        if pid <= 1:
            return
        with self._pid_lock:
            self._tracked_pids[int(pid)] = label
            self.persist_pids()

    def unregister_pid(self, pid: int) -> None:
        with self._pid_lock:
            self._tracked_pids.pop(int(pid), None)
            self.persist_pids()

    def persist_pids(self) -> None:
        self._pid_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._pid_file.with_suffix(self._pid_file.suffix + ".tmp")
        tmp.write_text(json.dumps({str(k): v for k, v in self._tracked_pids.items()}), encoding="utf-8")
        tmp.replace(self._pid_file)

    def reload_pids(self) -> None:
        try:
            raw = json.loads(self._pid_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        alive: dict[int, str] = {}
        for pid_str, label in raw.items():
            try:
                pid = int(pid_str)
                os.kill(pid, 0)
                alive[pid] = str(label)
            except (ProcessLookupError, PermissionError, ValueError):
                continue
        with self._pid_lock:
            self._tracked_pids = alive
            self.persist_pids()
        logger.info("PIDs TranscrIA rechargés: %d", len(alive))

    def force_free_gpu(self, gpu_index: int, allow_kill: bool = False) -> int:
        if not allow_kill:
            return 0
        freed_mb = self._kill_matching_gpu_processes(gpu_index, sig=signal.SIGTERM)
        time.sleep(2)
        freed_mb += self._kill_matching_gpu_processes(gpu_index, sig=signal.SIGKILL)
        return freed_mb

    def _kill_matching_gpu_processes(self, gpu_index: int, sig: signal.Signals) -> int:
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception as exc:
            logger.warning("force_gpu: nvidia-smi indisponible: %s", exc)
            return 0

        gpu_uuid = self._gpu_uuid(gpu_index)
        freed = 0
        with self._pid_lock:
            tracked = set(self._tracked_pids)
        for line in result.stdout.strip().splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) < 4:
                continue
            row_uuid, pid_raw, name, memory_raw = parts[:4]
            if gpu_uuid and row_uuid != gpu_uuid:
                continue
            try:
                pid = int(pid_raw)
                memory_mb = int(float(memory_raw))
            except ValueError:
                continue
            if pid <= 1 or pid in tracked or not self._match_kill_pattern(name):
                continue
            try:
                os.kill(pid, sig)
                freed += memory_mb
                logger.warning(
                    "force_gpu: signal %s envoyé à PID=%d (%s, %d Mo, gpu=%d)",
                    sig.name,
                    pid,
                    name,
                    memory_mb,
                    gpu_index,
                )
            except (ProcessLookupError, PermissionError):
                pass
        return freed

    def _gpu_uuid(self, gpu_index: int) -> str | None:
        nvidia_gpu_index = to_nvidia_smi_gpu_index(gpu_index)
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    f"--id={nvidia_gpu_index}",
                    "--query-gpu=uuid",
                    "--format=csv,noheader",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            value = result.stdout.strip().splitlines()
            return value[0].strip() if value else None
        except Exception:
            return None

    def _match_kill_pattern(self, process_name: str) -> bool:
        lower = process_name.lower()
        return any(pattern in lower for pattern in self._kill_patterns)

    def get_snapshot(self) -> dict:
        with self._alloc_lock:
            reservations_by_gpu = {
                gpu: [asdict(reservation) for reservation in reservations]
                for gpu, reservations in self._gpu_reservations.items()
            }
            gpus = []
            visible_devices = parse_cuda_visible_devices()
            for gpu in self.get_gpu_info():
                visible_gpu = to_visible_device_index(
                    gpu.get("id", 0),
                    visible_devices,
                    allow_remapped_ordinal=bool(gpu.get("cuda_visible_remapped")),
                )
                if visible_gpu is None:
                    continue
                reserved = self._reserved_vram_mb_locked(visible_gpu)
                free = self._get_available_vram_mb_locked(visible_gpu)
                gpus.append(
                    {
                        "id": visible_gpu,
                        "name": gpu.get("name", "inconnu"),
                        "reserved_vram_mb": reserved,
                        "free_vram_mb": free,
                        "reservations": reservations_by_gpu.get(visible_gpu, []),
                    }
                )
            with self._llm_owner_lock:
                llm_owner = self._llm_owner
            return {
                "gpus": gpus,
                "llm_locked": llm_owner is not None,
                "llm_owner": llm_owner,
                "tracked_pids": len(self._tracked_pids),
            }

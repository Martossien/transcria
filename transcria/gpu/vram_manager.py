import gc
import logging
import os
import subprocess
import time
from typing import IO

from transcria.gpu._port_utils import is_port_open as _check_port_open
from transcria.gpu.cuda_visible import (
    parse_cuda_visible_devices,
    to_nvidia_smi_gpu_index,
    to_visible_device_index,
)

logger = logging.getLogger(__name__)


class VRAMManager:
    """Cycle de vie GPU : libère, lance, utilise, arrête les modèles."""

    def __init__(self, config: dict, dashboard_url: str | None = None):
        self.config = config
        services = config.get("services", {})
        gpu_cfg = config.get("gpu", {})
        scheduling_cfg = config.get("workflow", {}).get("scheduling", {}) or {}
        self.arbitrage_llm_port: int = services.get(
            "arbitrage_llm_port",
            services.get("qwen_port", 8080),
        )
        self.llm_cleanup_ports: list[int] = list(
            services.get("llm_cleanup_ports", [services.get("vllm_port", 8000)])
        )
        self.vllm_port: int = self.llm_cleanup_ports[0] if self.llm_cleanup_ports else services.get("vllm_port", 8000)
        self.cohere_vram_mb: int = gpu_cfg.get("cohere_vram_mb", 6000)
        self.pyannote_vram_mb: int = gpu_cfg.get("pyannote_vram_mb", 2000)
        self.llm_vram_mb: int = gpu_cfg.get("llm_vram_mb", 60000)
        self.min_free_mb: int = gpu_cfg.get("min_free_vram_mb", 4000)
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
        _env_gpu = os.environ.get("TRANSCRIA_PREFERRED_GPU")
        self.preferred_gpu: int = int(_env_gpu) if _env_gpu else 0
        self.arbitrage_script: str = os.environ.get(
            "TRANSCRIA_ARBITRAGE_SCRIPT",
            services.get("arbitrage_script", "./scripts/launch_arbitrage.sh"),
        )
        self.stop_script: str = os.environ.get(
            "TRANSCRIA_STOP_SCRIPT",
            services.get("stop_script", "./scripts/stop_arbitrage_llm.sh"),
        )
        # Sortie du script d'arbitrage, capturée pour diagnostiquer les pannes de
        # démarrage (binaire introuvable, OOM GPU, tensor-split incompatible…).
        self.arbitrage_log_path: str = services.get("arbitrage_log_path") or (
            f"/tmp/arbitrage_llm_{self.arbitrage_llm_port}.log"
        )
        self.dashboard_url = (dashboard_url or services.get("dashboard_llm_url", "http://127.0.0.1:5001")).rstrip("/")
        self._loaded_models: dict[str, dict] = {}
        self._arbitrage_llm_pid: int | None = None

    # ── GPU Info ──────────────────────────────────────────

    def get_gpu_info(self) -> list[dict]:
        try:
            import requests
            resp = requests.get(f"{self.dashboard_url}/api/v1/gpus", timeout=5)
            resp.raise_for_status()
            return resp.json().get("gpus", [])
        except Exception:
            return self._get_gpu_info_fallback()

    def _get_gpu_info_fallback(self) -> list[dict]:
        gpus = []
        try:
            import torch
            if torch.cuda.is_available():
                for i in range(torch.cuda.device_count()):
                    free, total = torch.cuda.mem_get_info(i)
                    gpus.append({
                        "id": i, "name": torch.cuda.get_device_name(i),
                        "cuda_visible_remapped": True,
                        "memory": {"used": (total-free)/(1024**3), "free": free/(1024**3), "total": total/(1024**3)},
                    })
        except ImportError:
            pass
        return gpus

    def get_free_vram_mb(self, gpu_index: int = 0) -> int:
        visible_devices = parse_cuda_visible_devices()
        for g in self.get_gpu_info():
            if to_visible_device_index(
                g.get("id", 0),
                visible_devices,
                allow_remapped_ordinal=bool(g.get("cuda_visible_remapped")),
            ) == gpu_index:
                return int(g.get("memory", {}).get("free", 0) * 1024)
        return 0

    @staticmethod
    def _visible_cuda_device_count() -> int | None:
        """Retourne le nombre de GPUs CUDA visibles via CUDA_VISIBLE_DEVICES, ou None si non contraint."""
        visible = parse_cuda_visible_devices()
        if visible is None:
            return None
        return len(visible)

    def get_best_gpu(self, required_mb: int) -> int | None:
        visible_devices = parse_cuda_visible_devices()
        best_idx, best_free = None, 0
        for g in self.get_gpu_info():
            visible_gpu = to_visible_device_index(
                g.get("id", 0),
                visible_devices,
                allow_remapped_ordinal=bool(g.get("cuda_visible_remapped")),
            )
            if visible_gpu is None:
                continue
            free_mb = int(g.get("memory", {}).get("free", 0) * 1024)
            if free_mb >= required_mb + self.min_free_mb and free_mb > best_free:
                best_free, best_idx = free_mb, visible_gpu
        return best_idx

    def _log_all_gpus(self, label: str = "") -> None:
        """Logue la VRAM libre de chaque GPU — utile pour le débogage des basculements."""
        gpus = self.get_gpu_info()
        prefix = f"[{label}] " if label else ""
        for g in gpus:
            free_mb = int(g.get("memory", {}).get("free", 0) * 1024)
            used_mb = int(g.get("memory", {}).get("used", 0) * 1024)
            total_mb = int(g.get("memory", {}).get("total", 0) * 1024)
            logger.info(
                "%sGPU %d — libre: %d Mo / total: %d Mo (utilisé: %d Mo) [%s]",
                prefix, g.get("id", "?"), free_mb, total_mb, used_mb,
                g.get("name", "inconnu"),
            )

    def ensure_free(self, required_mb: int, preferred_gpu: int | None = None) -> int | None:
        if preferred_gpu is None:
            preferred_gpu = self.preferred_gpu
        free = self.get_free_vram_mb(preferred_gpu)
        logger.info(
            "VRAM GPU %d: %d Mo libre, besoin %d Mo",
            preferred_gpu, free, required_mb,
        )
        if free >= required_mb + self.min_free_mb:
            logger.info("GPU %d sélectionné (%d Mo disponibles)", preferred_gpu, free)
            return preferred_gpu

        # GPU préféré insuffisant — état de tous les GPUs avant basculement
        logger.info(
            "GPU %d insuffisant (%d Mo < besoin %d Mo) — scan de tous les GPUs",
            preferred_gpu, free, required_mb + self.min_free_mb,
        )
        self._log_all_gpus(label="scan")

        best = self.get_best_gpu(required_mb)
        if best is not None:
            best_free = self.get_free_vram_mb(best)
            logger.info(
                "Basculement GPU %d → GPU %d (%d Mo libre)",
                preferred_gpu, best, best_free,
            )
            return best

        logger.warning(
            "Aucun GPU avec %d Mo libre — tentative libération VRAM sur GPU %d",
            required_mb, preferred_gpu,
        )
        self._free_memory(preferred_gpu)
        gc.collect()
        try:
            import torch
            torch.cuda.empty_cache()
        except ImportError:
            pass
        time.sleep(1)
        free_after = self.get_free_vram_mb(preferred_gpu)
        logger.info("GPU %d après libération: %d Mo libre", preferred_gpu, free_after)
        if free_after >= required_mb + self.min_free_mb:
            logger.info("GPU %d sélectionné après libération", preferred_gpu)
            return preferred_gpu

        best_after = self.get_best_gpu(required_mb)
        if best_after is not None:
            best_after_free = self.get_free_vram_mb(best_after)
            logger.info(
                "Basculement GPU %d → GPU %d après libération (%d Mo libre)",
                preferred_gpu, best_after, best_after_free,
            )
        else:
            logger.error(
                "Aucun GPU disponible avec %d Mo après libération — état final:",
                required_mb,
            )
            self._log_all_gpus(label="échec")
        return best_after

    def _free_memory(self, gpu_index: int) -> None:
        """Tente de libérer la VRAM en tuant les processus GPU > 4 Go."""
        import signal as _sig
        nvidia_gpu_index = to_nvidia_smi_gpu_index(gpu_index)
        try:
            result = subprocess.run(
                ["nvidia-smi", "-i", str(nvidia_gpu_index),
                 "--query-compute-apps=pid,process_name,used_gpu_memory",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.strip().split("\n"):
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 3:
                    try:
                        vram_mb = float(parts[2])
                        pid = int(parts[0])
                        process_name = parts[1]
                        if (
                            vram_mb > 4000
                            and pid > 1
                            and self._matches_kill_pattern(process_name)
                        ):
                            logger.warning("Libération VRAM: kill PID %s (%s, %d Mo)", parts[0], parts[1], int(vram_mb))
                            os.kill(pid, _sig.SIGTERM)
                    except (ValueError, ProcessLookupError, PermissionError):
                        pass
            time.sleep(2)
            result2 = subprocess.run(
                ["nvidia-smi", "-i", str(nvidia_gpu_index),
                 "--query-compute-apps=pid,process_name,used_gpu_memory",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10,
            )
            for line in result2.stdout.strip().split("\n"):
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 3:
                    try:
                        vram_mb = float(parts[2])
                        pid = int(parts[0])
                        process_name = parts[1]
                        if (
                            vram_mb > 4000
                            and pid > 1
                            and self._matches_kill_pattern(process_name)
                        ):
                            logger.warning("SIGKILL PID %s (%d Mo)", parts[0], int(vram_mb))
                            os.kill(pid, _sig.SIGKILL)
                    except (ValueError, ProcessLookupError, PermissionError):
                        pass
        except Exception:
            pass

    def _matches_kill_pattern(self, process_name: str) -> bool:
        lower = process_name.lower()
        return any(pattern in lower for pattern in self._kill_patterns)

    # ── Model tracking ────────────────────────────────────

    def track_model(self, name: str, gpu: int, vram_mb: int) -> None:
        self._loaded_models[name] = {"gpu": gpu, "vram_mb": vram_mb, "loaded_at": time.time()}
        logger.info("Modèle %s chargé sur GPU %d (~%d Mo)", name, gpu, vram_mb)

    def untrack_model(self, name: str) -> None:
        self._loaded_models.pop(name, None)

    def offload_all(self) -> None:
        self._loaded_models.clear()
        gc.collect()
        try:
            import torch
            torch.cuda.empty_cache()
        except ImportError:
            pass
        logger.info("Cache CUDA vidé")

    # ── Service lifecycle ─────────────────────────────────

    def is_arbitrage_llm_running(self) -> bool:
        """Retourne True si la LLM d'arbitrage répond à l'API attendue.

        `lsof` peut être indisponible ou incomplet selon le contexte systemd/sandbox.
        L'autorité fonctionnelle est donc l'API OpenAI-compatible elle-même.
        """
        if VRAMManager.is_port_open(self.arbitrage_llm_port):
            return True
        try:
            result = subprocess.run(
                ["lsof", "-ti", f"tcp:{self.arbitrage_llm_port}", "-sTCP:LISTEN"],
                capture_output=True, text=True, timeout=5,
            )
            return bool(result.stdout.strip())
        except Exception:
            return False

    def _get_port_pid(self, port: int) -> str:
        """Retourne le(s) PID qui écoutent sur ce port, pour les logs."""
        try:
            result = subprocess.run(
                ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                capture_output=True, text=True, timeout=5
            )
            pids = [p.strip() for p in result.stdout.strip().split("\n") if p.strip()]
            return ",".join(pids) if pids else "inconnu"
        except Exception:
            return "inconnu"

    def _kill_port(self, port: int) -> bool:
        """Tue uniquement le processus qui écoute sur ce port (LISTEN)."""
        import signal
        try:
            result = subprocess.run(
                ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"], capture_output=True, text=True, timeout=5
            )
            pids = [int(p) for p in result.stdout.strip().split("\n") if p.strip().isdigit()]
            if not pids:
                return True
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGTERM)
                    logger.info("SIGTERM → PID %d (LISTEN port %d)", pid, port)
                except (ProcessLookupError, PermissionError):
                    pass
            time.sleep(3)
            result2 = subprocess.run(
                ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"], capture_output=True, text=True, timeout=5
            )
            survivors = [int(p) for p in result2.stdout.strip().split("\n") if p.strip().isdigit()]
            for pid in survivors:
                try:
                    os.kill(pid, signal.SIGKILL)
                    logger.info("SIGKILL → PID %d (LISTEN port %d)", pid, port)
                except (ProcessLookupError, PermissionError):
                    pass
            return True
        except Exception as exc:
            logger.warning("Échec kill port %d: %s", port, exc)
            return False

    def stop_cleanup_llm_ports(self) -> bool:
        """Libère les ports de backends LLM concurrents configurés."""
        ok = True
        for port in self.llm_cleanup_ports:
            logger.info("Arrêt backend LLM concurrent port %d...", port)
            ok = self._kill_port(port) and ok
        gc.collect()
        try:
            import torch
            torch.cuda.empty_cache()
        except ImportError:
            pass
        return ok

    def stop_vllm_port_8000(self) -> bool:
        """Alias compatibilité : utiliser stop_cleanup_llm_ports()."""
        return self.stop_cleanup_llm_ports()

    def launch_arbitrage_llm(self) -> bool:
        """Lance la LLM d'arbitrage via le script configuré."""
        if not os.path.isfile(self.arbitrage_script):
            logger.error("Script d'arbitrage introuvable: %s", self.arbitrage_script)
            return False

        if self.is_port_open(self.arbitrage_llm_port):
            # Vérifier que le serveur existant répond bien à l'API avant de l'utiliser
            try:
                import urllib.request
                urllib.request.urlopen(
                    f"http://127.0.0.1:{self.arbitrage_llm_port}/v1/models", timeout=5
                )
                pid_info = self._get_port_pid(self.arbitrage_llm_port)
                logger.info(
                    "LLM d'arbitrage déjà active sur port %d (PID %s) — réutilisation sans redémarrage",
                    self.arbitrage_llm_port, pid_info,
                )
                return True
            except Exception:
                logger.info("Port %d occupé mais /v1/models ne répond pas — nettoyage et relance",
                            self.arbitrage_llm_port)
                self._kill_port(self.arbitrage_llm_port)
                time.sleep(3)

        logger.info(
            "Lancement LLM d'arbitrage via %s (sortie → %s)...",
            self.arbitrage_script, self.arbitrage_log_path,
        )
        log_fh: IO[bytes] | int
        try:
            log_fh = open(self.arbitrage_log_path, "ab")
        except OSError as exc:
            logger.warning(
                "Impossible d'ouvrir le log de lancement %s (%s) — sortie non capturée",
                self.arbitrage_log_path, exc,
            )
            log_fh = subprocess.DEVNULL
        try:
            proc = subprocess.Popen(
                ["/bin/bash", self.arbitrage_script],
                stdout=log_fh, stderr=log_fh,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            self._arbitrage_llm_pid = proc.pid
            try:
                from transcria.queue.allocator import GPUAllocator

                GPUAllocator.get_instance(self.config).register_pid(
                    proc.pid, "arbitrage_llm"
                )
            except Exception:
                logger.debug("Tracking PID LLM indisponible", exc_info=True)
            logger.info(
                "LLM d'arbitrage lancée — PID %d, attente du port %d...",
                proc.pid,
                self.arbitrage_llm_port,
            )
            return self._wait_for_port(
                self.arbitrage_llm_port, timeout=600,
                proc=proc, log_path=self.arbitrage_log_path,
            )
        except Exception as exc:
            logger.error("Échec lancement LLM d'arbitrage: %s", exc)
            return False
        finally:
            if log_fh is not subprocess.DEVNULL:
                try:
                    log_fh.close()  # type: ignore[union-attr]
                except OSError:
                    pass

    def ensure_arbitrage_llm_ready(self, expected_model_id: str | None = None) -> bool:
        """S'assure que la LLM d'arbitrage est opérationnelle et utilise le bon modèle.

        Trois cas tracés explicitement dans les logs :
          A — LLM saine + bon modèle  → réutilisation directe, zéro redémarrage
          B — LLM saine + mauvais modèle → redémarrage (warning logué)
          C — LLM absente ou non saine → libération GPU + lancement depuis zéro
        """
        import requests

        active_model_id: str | None = None
        server_healthy = False

        try:
            r = requests.get(
                f"http://127.0.0.1:{self.arbitrage_llm_port}/v1/models", timeout=5
            )
            if r.status_code == 200:
                data = r.json().get("data", [])
                if data:
                    active_model_id = data[0].get("id", "")
                    r2 = requests.post(
                        f"http://127.0.0.1:{self.arbitrage_llm_port}/v1/completions",
                        json={
                            "model": active_model_id,
                            "prompt": "Bonjour",
                            "max_tokens": 5,
                            "temperature": 0,
                        },
                        timeout=30,
                    )
                    if r2.status_code == 200:
                        choices = r2.json().get("choices", [])
                        server_healthy = (
                            len(choices) > 0
                            and len(choices[0].get("text", "")) > 0
                        )
        except Exception as exc:
            logger.debug("Sondage LLM d'arbitrage port %d: %s", self.arbitrage_llm_port, exc)

        pid_info = self._get_port_pid(self.arbitrage_llm_port)

        # Cas A : saine + bon modèle → réutilisation, aucun redémarrage
        if server_healthy and (
            expected_model_id is None or active_model_id == expected_model_id
        ):
            logger.info(
                "[arbitrage_llm] CAS A — LLM active et saine, réutilisation directe "
                "(port %d, PID %s, model: %s)",
                self.arbitrage_llm_port, pid_info, active_model_id,
            )
            return True

        # Cas B : saine mais mauvais modèle → redémarrage avec warning
        if server_healthy and expected_model_id and active_model_id != expected_model_id:
            logger.warning(
                "[arbitrage_llm] CAS B — Mauvais modèle actif sur port %d "
                "(trouvé: %s, attendu: %s, PID %s) — redémarrage forcé",
                self.arbitrage_llm_port, active_model_id, expected_model_id, pid_info,
            )
        else:
            # Cas C : port fermé ou inférence échouée → lancement depuis zéro
            logger.info(
                "[arbitrage_llm] CAS C — LLM non disponible sur port %d "
                "(model détecté: %s, health: %s) — libération GPU et lancement",
                self.arbitrage_llm_port, active_model_id or "aucun", server_healthy,
            )

        self.stop_cleanup_llm_ports()
        self.stop_arbitrage_llm()
        return self.launch_arbitrage_llm()

    def stop_arbitrage_llm(self) -> bool:
        """Arrête la LLM d'arbitrage via le script d'arrêt, puis kill port en fallback."""
        logger.info("Arrêt LLM d'arbitrage port %d...", self.arbitrage_llm_port)
        if os.path.isfile(self.stop_script):
            try:
                subprocess.run(
                    ["/bin/bash", self.stop_script],
                    capture_output=True, text=True, timeout=30,
                )
                logger.info("Script d'arrêt exécuté: %s", self.stop_script)
            except Exception as exc:
                logger.warning("Échec script d'arrêt: %s", exc)
        port_ok = self._kill_port(self.arbitrage_llm_port)
        if self._arbitrage_llm_pid is not None:
            try:
                from transcria.queue.allocator import GPUAllocator

                GPUAllocator.get_instance(self.config).unregister_pid(
                    self._arbitrage_llm_pid
                )
            except Exception:
                logger.debug("Nettoyage tracking PID LLM indisponible", exc_info=True)
        self._arbitrage_llm_pid = None
        gc.collect()
        try:
            import torch
            torch.cuda.empty_cache()
        except ImportError:
            pass
        return port_ok

    def free_all_gpus(self) -> bool:
        """Libère tous les GPUs visibles : arrête tout modèle chargé."""
        n = len(self.get_gpu_info())
        logger.info("Libération de %d GPU(s) visible(s)...", n)
        ok1 = self.stop_cleanup_llm_ports()
        ok2 = self.stop_arbitrage_llm()
        self.offload_all()
        time.sleep(2)
        gpus = self.get_gpu_info()
        for g in gpus:
            free = int(g["memory"]["free"] * 1024)
            logger.info("GPU %d: %d Mo libre après libération", g["id"], free)
        return ok1 and ok2

    @staticmethod
    def is_port_open(port: int) -> bool:
        return _check_port_open(port)

    @staticmethod
    def _diagnostic_tail(log_path: str | None, n_lines: int = 25) -> str:
        """Renvoie les dernières lignes du log de lancement, pour expliquer une panne.

        Sans ce contexte, un échec de démarrage du serveur LLM reste invisible :
        le process sort en silence et l'on n'observe qu'un timeout d'attente du port.
        """
        if not log_path or not os.path.isfile(log_path):
            return f"(aucun log de lancement disponible: {log_path or 'sortie non capturée'})"
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError as exc:
            return f"(impossible de lire {log_path}: {exc})"
        tail = "".join(lines[-n_lines:]).strip()
        if not tail:
            return f"(log de lancement vide: {log_path})"
        return f"Dernières lignes de {log_path}:\n{tail}"

    @staticmethod
    def _wait_for_port(
        port: int,
        timeout: int = 300,
        *,
        proc: "subprocess.Popen | None" = None,
        log_path: str | None = None,
    ) -> bool:
        start = time.time()
        deadline = start + timeout
        while time.time() < deadline:
            if VRAMManager.is_port_open(port):
                logger.info("Port %d répond après %.0fs", port, time.time() - start)
                return True
            # Mort précoce du process lancé : inutile d'attendre tout le timeout —
            # on remonte le code de sortie et le log pour expliquer la panne.
            if proc is not None and proc.poll() is not None:
                logger.error(
                    "Le serveur LLM s'est arrêté avant d'ouvrir le port %d "
                    "(code de sortie=%s). %s",
                    port, proc.returncode, VRAMManager._diagnostic_tail(log_path),
                )
                return False
            time.sleep(5)
        logger.error(
            "Timeout attente port %d après %ds — le serveur LLM ne répond pas. %s",
            port, timeout, VRAMManager._diagnostic_tail(log_path),
        )
        return False

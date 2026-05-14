import gc
import logging
import os
import subprocess
import time

logger = logging.getLogger(__name__)


import os

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class VRAMManager:
    """Cycle de vie GPU : libère, lance, utilise, arrête les modèles."""

    COHERE_VRAM_MB = 6000
    PYANNOTE_VRAM_MB = 2000
    QWEN35_VRAM_MB = 60000
    MIN_FREE_MB = 4000

    QWEN_PORT = 8080
    VLLM_PORT = 8000

    ARBITRAGE_SCRIPT = os.environ.get("TRANSCRIA_ARBITRAGE_SCRIPT", "/root/launch_arbitrage2.sh")
    STOP_SCRIPT = os.environ.get("TRANSCRIA_STOP_SCRIPT", "/root/stop_qwen36_27b_vllm.sh")

    def __init__(self, dashboard_url: str = "http://127.0.0.1:5001"):
        self.dashboard_url = dashboard_url.rstrip("/")
        self._loaded_models: dict[str, dict] = {}
        self._qwen_pid: int | None = None

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
                        "memory": {"used": (total-free)/(1024**3), "free": free/(1024**3), "total": total/(1024**3)},
                    })
        except ImportError:
            pass
        return gpus

    def get_free_vram_mb(self, gpu_index: int = 0) -> int:
        for g in self.get_gpu_info():
            if g.get("id") == gpu_index:
                return int(g.get("memory", {}).get("free", 0) * 1024)
        return 0

    def get_best_gpu(self, required_mb: int) -> int | None:
        best_idx, best_free = None, 0
        for g in self.get_gpu_info():
            free_mb = int(g.get("memory", {}).get("free", 0) * 1024)
            if free_mb >= required_mb + self.MIN_FREE_MB and free_mb > best_free:
                best_free, best_idx = free_mb, g["id"]
        return best_idx

    def ensure_free(self, required_mb: int, preferred_gpu: int = 0) -> int | None:
        free = self.get_free_vram_mb(preferred_gpu)
        logger.info("VRAM GPU %d: %d Mo libre, besoin %d Mo", preferred_gpu, free, required_mb)
        if free >= required_mb + self.MIN_FREE_MB:
            return preferred_gpu
        best = self.get_best_gpu(required_mb)
        if best is not None:
            return best
        self._free_memory(preferred_gpu)
        gc.collect()
        try:
            import torch; torch.cuda.empty_cache()
        except ImportError:
            pass
        time.sleep(1)
        free_after = self.get_free_vram_mb(preferred_gpu)
        if free_after >= required_mb + self.MIN_FREE_MB:
            return preferred_gpu
        return self.get_best_gpu(required_mb)

    def _free_memory(self, gpu_index: int) -> None:
        """Tente de libérer la VRAM en tuant les processus GPU > 4 Go."""
        import signal as _sig
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-compute-apps=pid,process_name,used_gpu_memory",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.strip().split("\n"):
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 3:
                    try:
                        vram_mb = float(parts[2])
                        pid = int(parts[0])
                        if vram_mb > 4000 and pid > 1:
                            logger.warning("Libération VRAM: kill PID %s (%s, %d Mo)", parts[0], parts[1], int(vram_mb))
                            os.kill(pid, _sig.SIGTERM)
                    except (ValueError, ProcessLookupError, PermissionError):
                        pass
            time.sleep(2)
            result2 = subprocess.run(
                ["nvidia-smi", "--query-compute-apps=pid,used_gpu_memory",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10,
            )
            for line in result2.stdout.strip().split("\n"):
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 2:
                    try:
                        vram_mb = float(parts[1])
                        pid = int(parts[0])
                        if vram_mb > 4000 and pid > 1:
                            logger.warning("SIGKILL PID %s (%d Mo)", parts[0], int(vram_mb))
                            os.kill(pid, _sig.SIGKILL)
                    except (ValueError, ProcessLookupError, PermissionError):
                        pass
        except Exception:
            pass

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
            import torch; torch.cuda.empty_cache()
        except ImportError:
            pass
        logger.info("Cache CUDA vidé")

    # ── Service lifecycle ─────────────────────────────────

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

    def stop_vllm_port_8000(self) -> bool:
        """Tue le vLLM sur le port 8000 (Voxtral Mini 4B)."""
        logger.info("Arrêt vLLM port 8000...")
        ok = self._kill_port(self.VLLM_PORT)
        gc.collect()
        try: import torch; torch.cuda.empty_cache()
        except ImportError: pass
        return ok

    def launch_qwen_35b(self) -> bool:
        """Lance Qwen 3.6 35B UD-Q8_XL via le script d'arbitrage (port 8080, 2 GPUs)."""
        if not os.path.isfile(self.ARBITRAGE_SCRIPT):
            logger.error("Script d'arbitrage introuvable: %s", self.ARBITRAGE_SCRIPT)
            return False

        if self.is_port_open(self.QWEN_PORT):
            logger.info("Port %d déjà occupé — nettoyage avant lancement", self.QWEN_PORT)
            self._kill_port(self.QWEN_PORT)
            time.sleep(3)

        logger.info("Lancement Qwen 35B via %s...", self.ARBITRAGE_SCRIPT)
        try:
            proc = subprocess.Popen(
                ["/bin/bash", self.ARBITRAGE_SCRIPT],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self._qwen_pid = proc.pid
            logger.info("Qwen 35B lancé — PID %d, attente du port 8080...", proc.pid)
            return self._wait_for_port(self.QWEN_PORT, timeout=600)
        except Exception as exc:
            logger.error("Échec lancement Qwen 35B: %s", exc)
            return False

    def stop_qwen_35b(self) -> bool:
        """Arrête Qwen 35B sur le port 8080."""
        logger.info("Arrêt Qwen 35B port 8080...")
        ok = self._kill_port(self.QWEN_PORT)
        self._qwen_pid = None
        gc.collect()
        try: import torch; torch.cuda.empty_cache()
        except ImportError: pass
        return ok

    def free_all_gpus(self) -> bool:
        """Libère les 2 GPUs : arrête tout modèle chargé."""
        logger.info("Libération des 2 GPUs...")
        ok1 = self.stop_vllm_port_8000()
        ok2 = self.stop_qwen_35b()
        self.offload_all()
        time.sleep(2)
        gpus = self.get_gpu_info()
        for g in gpus:
            free = int(g["memory"]["free"] * 1024)
            logger.info("GPU %d: %d Mo libre après libération", g["id"], free)
        return ok1 and ok2

    @staticmethod
    def is_port_open(port: int) -> bool:
        try:
            import requests, json
            r = requests.get(f"http://127.0.0.1:{port}/v1/models", timeout=5)
            if r.status_code != 200:
                return False
            data = r.json()
            if not data.get("data"):
                return False
            model_id = data["data"][0].get("id", "")
            # Vérifier que le modèle répond réellement aux inférences
            r2 = requests.post(
                f"http://127.0.0.1:{port}/v1/completions",
                json={"model": model_id, "prompt": "Bonjour", "max_tokens": 5, "temperature": 0},
                timeout=30,
            )
            if r2.status_code == 200:
                choices = r2.json().get("choices", [])
                return len(choices) > 0 and len(choices[0].get("text", "")) > 0
            return False
        except Exception:
            return False

    @staticmethod
    def _wait_for_port(port: int, timeout: int = 300) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if VRAMManager.is_port_open(port):
                logger.info("Port %d répond après %.0fs", port, time.time() - (deadline - timeout))
                return True
            time.sleep(5)
        logger.error("Timeout attente port %d après %ds", port, timeout)
        return False

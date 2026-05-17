import gc
import logging
import os
import subprocess
import time

logger = logging.getLogger(__name__)


class VRAMManager:
    """Cycle de vie GPU : libère, lance, utilise, arrête les modèles."""

    def __init__(self, config: dict, dashboard_url: str | None = None):
        self.config = config
        services = config.get("services", {})
        gpu_cfg = config.get("gpu", {})
        self.qwen_port: int = services.get("qwen_port", 8080)
        self.vllm_port: int = services.get("vllm_port", 8000)
        self.cohere_vram_mb: int = gpu_cfg.get("cohere_vram_mb", 6000)
        self.pyannote_vram_mb: int = gpu_cfg.get("pyannote_vram_mb", 2000)
        self.llm_vram_mb: int = gpu_cfg.get("llm_vram_mb", 60000)
        self.min_free_mb: int = gpu_cfg.get("min_free_vram_mb", 4000)
        self.arbitrage_script: str = os.environ.get(
            "TRANSCRIA_ARBITRAGE_SCRIPT",
            services.get("arbitrage_script", "./scripts/launch_arbitrage.sh"),
        )
        self.stop_script: str = os.environ.get(
            "TRANSCRIA_STOP_SCRIPT",
            services.get("stop_script", "./scripts/stop_qwen.sh"),
        )
        self.dashboard_url = (dashboard_url or services.get("dashboard_llm_url", "http://127.0.0.1:5001")).rstrip("/")
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
            if free_mb >= required_mb + self.min_free_mb and free_mb > best_free:
                best_free, best_idx = free_mb, g["id"]
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

    def ensure_free(self, required_mb: int, preferred_gpu: int = 0) -> int | None:
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
            import torch; torch.cuda.empty_cache()
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

    def is_arbitrage_llm_running(self) -> bool:
        """Retourne True si un processus écoute sur le port de la LLM d'arbitrage."""
        try:
            result = subprocess.run(
                ["lsof", "-ti", f"tcp:{self.qwen_port}", "-sTCP:LISTEN"],
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

    def stop_vllm_port_8000(self) -> bool:
        """Tue le vLLM sur le port configuré (défaut 8000)."""
        logger.info("Arrêt vLLM port %d...", self.vllm_port)
        ok = self._kill_port(self.vllm_port)
        gc.collect()
        try: import torch; torch.cuda.empty_cache()
        except ImportError: pass
        return ok

    def launch_qwen_35b(self) -> bool:
        """Lance Qwen 3.6 35B UD-Q8_XL via le script d'arbitrage (port configuré, 2 GPUs)."""
        if not os.path.isfile(self.arbitrage_script):
            logger.error("Script d'arbitrage introuvable: %s", self.arbitrage_script)
            return False

        if self.is_port_open(self.qwen_port):
            # Vérifier que le serveur existant répond bien à l'API avant de l'utiliser
            try:
                import urllib.request
                urllib.request.urlopen(
                    f"http://127.0.0.1:{self.qwen_port}/v1/models", timeout=5
                )
                pid_info = self._get_port_pid(self.qwen_port)
                logger.info(
                    "Qwen 35B déjà actif sur port %d (PID %s) — réutilisation sans redémarrage",
                    self.qwen_port, pid_info,
                )
                return True
            except Exception:
                logger.info("Port %d occupé mais /v1/models ne répond pas — nettoyage et relance",
                            self.qwen_port)
                self._kill_port(self.qwen_port)
                time.sleep(3)

        logger.info("Lancement Qwen 35B via %s...", self.arbitrage_script)
        try:
            proc = subprocess.Popen(
                ["/bin/bash", self.arbitrage_script],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self._qwen_pid = proc.pid
            logger.info("Qwen 35B lancé — PID %d, attente du port %d...", proc.pid, self.qwen_port)
            return self._wait_for_port(self.qwen_port, timeout=600)
        except Exception as exc:
            logger.error("Échec lancement Qwen 35B: %s", exc)
            return False

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
                f"http://127.0.0.1:{self.qwen_port}/v1/models", timeout=5
            )
            if r.status_code == 200:
                data = r.json().get("data", [])
                if data:
                    active_model_id = data[0].get("id", "")
                    r2 = requests.post(
                        f"http://127.0.0.1:{self.qwen_port}/v1/completions",
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
            logger.debug("Sondage LLM d'arbitrage port %d: %s", self.qwen_port, exc)

        pid_info = self._get_port_pid(self.qwen_port)

        # Cas A : saine + bon modèle → réutilisation, aucun redémarrage
        if server_healthy and (
            expected_model_id is None or active_model_id == expected_model_id
        ):
            logger.info(
                "[arbitrage_llm] CAS A — LLM active et saine, réutilisation directe "
                "(port %d, PID %s, model: %s)",
                self.qwen_port, pid_info, active_model_id,
            )
            return True

        # Cas B : saine mais mauvais modèle → redémarrage avec warning
        if server_healthy and expected_model_id and active_model_id != expected_model_id:
            logger.warning(
                "[arbitrage_llm] CAS B — Mauvais modèle actif sur port %d "
                "(trouvé: %s, attendu: %s, PID %s) — redémarrage forcé",
                self.qwen_port, active_model_id, expected_model_id, pid_info,
            )
        else:
            # Cas C : port fermé ou inférence échouée → lancement depuis zéro
            logger.info(
                "[arbitrage_llm] CAS C — LLM non disponible sur port %d "
                "(model détecté: %s, health: %s) — libération GPU et lancement",
                self.qwen_port, active_model_id or "aucun", server_healthy,
            )

        self.stop_vllm_port_8000()
        self.stop_qwen_35b()
        return self.launch_qwen_35b()

    def stop_qwen_35b(self) -> bool:
        """Arrête Qwen 35B via le script d'arrêt, puis kill port en fallback."""
        logger.info("Arrêt Qwen 35B port %d...", self.qwen_port)
        if os.path.isfile(self.stop_script):
            try:
                subprocess.run(
                    ["/bin/bash", self.stop_script],
                    capture_output=True, text=True, timeout=30,
                )
                logger.info("Script d'arrêt exécuté: %s", self.stop_script)
            except Exception as exc:
                logger.warning("Échec script d'arrêt: %s", exc)
        port_ok = self._kill_port(self.qwen_port)
        self._qwen_pid = None
        gc.collect()
        try: import torch; torch.cuda.empty_cache()
        except ImportError: pass
        return port_ok

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

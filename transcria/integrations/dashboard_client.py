import logging

import requests

logger = logging.getLogger(__name__)


class DashboardClient:
    # Timeout court par défaut : le dashboard tourne en local (réponse en ms quand il
    # est présent). Un timeout long ferait pendre la page Système quand il est absent
    # (cas courant en all-in-one). get_system_status court-circuite dès le 1er échec.
    def __init__(self, base_url: str = "http://127.0.0.1:5001", timeout: int = 3):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str) -> dict:
        try:
            resp = requests.get(f"{self.base_url}{path}", timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.debug("Dashboard API indisponible (%s): %s", path, exc)
            return {"error": str(exc), "available": False}

    def get_metrics(self) -> dict:
        return self._get("/api/v1/metrics")

    def get_gpus(self) -> dict:
        return self._get("/api/v1/gpus")

    def get_services(self) -> dict:
        return self._get("/api/v1/services")

    def get_gpu_processes(self) -> dict:
        return self._get("/api/v1/gpus/processes")

    @staticmethod
    def _unavailable_status() -> dict:
        return {
            "cpu": {}, "ram": {}, "gpus": [], "services": {},
            "gpu_processes": {}, "model": "", "available": False,
        }

    def get_system_status(self) -> dict:
        # Court-circuit : si le 1er appel échoue, le dashboard est injoignable —
        # inutile d'enchaîner 3 autres requêtes qui timeout (page Système qui pend).
        metrics = self.get_metrics()
        if "error" in metrics:
            logger.warning(
                "Dashboard de ressources injoignable (%s) — page Système en mode dégradé.",
                self.base_url,
            )
            return self._unavailable_status()

        gpus = self.get_gpus()
        services = self.get_services()
        processes = self.get_gpu_processes()

        return {
            "cpu": metrics.get("cpu", {}),
            "ram": metrics.get("ram", {}),
            "gpus": gpus.get("gpus", []),
            "services": services.get("services", {}),
            "gpu_processes": processes,
            "model": metrics.get("model", ""),
            "available": True,
        }

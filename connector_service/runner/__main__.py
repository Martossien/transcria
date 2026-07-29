"""`python -m connector_service.runner` — le meeting-runner de PRODUCTION.

Branche la boucle testée (`daemon`) sur le monde réel : HTTP vers le portail (aiohttp absent
volontairement — urllib dans un thread suffit à un démon qui parle toutes les 30 s) et
`docker run` construit par `commands.docker_argv`. Config : TRANSCRIA_RUNNER_CONFIG (YAML).

    TRANSCRIA_RUNNER_CONFIG=/etc/transcria/runner.yaml python -m connector_service.runner

Codes de sortie : 0 arrêt demandé, 3 configuration invalide.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import urllib.error
import urllib.request

from connector_service.runner.commands import docker_argv
from connector_service.runner.config import RunnerConfigError, load_runner_config
from connector_service.runner.daemon import MeetingRunnerDaemon

logger = logging.getLogger("connector_service.runner")


def _http_post(base_url: str, token: str):
    def _sync(path: str, payload: dict) -> tuple[int, dict]:
        req = urllib.request.Request(
            base_url.rstrip("/") + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8") or "{}")
            except ValueError:
                return exc.code, {}
        except (urllib.error.URLError, TimeoutError) as exc:
            logger.warning("portail injoignable (%s) : %s", path, exc)
            return 0, {}

    async def post(path: str, payload: dict) -> tuple[int, dict]:
        return await asyncio.to_thread(_sync, path, payload)
    return post


def _docker_launcher(cfg):
    async def launch(intent: dict):
        argv, env = docker_argv(intent, portal_url=cfg.portal_url, token=cfg.token,
                                images=cfg.images)
        return await asyncio.create_subprocess_exec(
            *argv, env={**os.environ, **env},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    return launch


async def _amain() -> int:
    try:
        cfg = load_runner_config()
    except RunnerConfigError as exc:
        logger.error("%s", exc)
        return 3
    daemon = MeetingRunnerDaemon(cfg, post=_http_post(cfg.portal_url, cfg.token),
                                 launch=_docker_launcher(cfg))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, daemon.stop)
    logger.info("meeting-runner « %s » démarré — portail %s, plateformes %s",
                cfg.runner_name, cfg.portal_url, list(cfg.platforms))
    await daemon.run_forever()
    return 0


def main() -> int:
    logging.basicConfig(level=os.environ.get("RUNNER_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return asyncio.run(_amain())


if __name__ == "__main__":
    sys.exit(main())

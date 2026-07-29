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


def probe_local_images(images: dict) -> list[dict]:
    """Présence RÉELLE des images de bot (docker image inspect) — annoncée au heartbeat,
    affichée par la check-list admin (« Image de bot disponible »)."""
    import subprocess

    from connector_service.runner.commands import DEFAULT_IMAGES

    out = []
    for provider, image in {**DEFAULT_IMAGES, **(images or {})}.items():
        probe = subprocess.run(["docker", "image", "inspect", image],
                               capture_output=True)
        out.append({"name": image, "provider": provider,
                    "present": probe.returncode == 0})
    return out


_DOCKERFILES = {"jitsi": "Dockerfile.bot", "zoom-sdk": "Dockerfile.zoom-sdk"}


async def _image_autoheal(cfg) -> None:
    """AUTO-RÉPARATION des images de bot (décision utilisateur : « si l'image est absente,
    il faut la créer sans intervention ») : toutes les 5 min, pour chaque image absente —
    `docker pull` (images GHCR épinglées) puis, en repli, `docker build` depuis le dépôt
    (les Dockerfiles des bots, WorkingDirectory de l'unité). Journalisé, jamais bloquant :
    la check-list admin passe au vert toute seule quand c'est prêt."""
    from connector_service.runner.commands import DEFAULT_IMAGES

    while True:
        for provider, image in {**DEFAULT_IMAGES, **(cfg.images or {})}.items():
            probe = await asyncio.create_subprocess_exec(
                "docker", "image", "inspect", image,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            if await probe.wait() == 0:
                continue
            logger.info("image %s absente — tentative de pull…", image)
            pull = await asyncio.create_subprocess_exec(
                "docker", "pull", image,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            if await pull.wait() == 0:
                logger.info("image %s tirée", image)
                continue
            dockerfile = _DOCKERFILES.get(provider)
            if dockerfile and os.path.exists(dockerfile):
                logger.info("pull impossible — construction locale de %s (%s)…", image, dockerfile)
                build = await asyncio.create_subprocess_exec(
                    "docker", "build", "-q", "-f", dockerfile, "-t", image, ".",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.STDOUT)
                logger.info("construction de %s : %s", image,
                            "OK" if await build.wait() == 0 else "ÉCHEC (voir docker build à la main)")
        await asyncio.sleep(300)


def _docker_launcher(cfg):
    async def launch(intent: dict):
        argv, env = docker_argv(intent, portal_url=cfg.portal_url, token=cfg.token,
                                images=cfg.images)
        return await asyncio.create_subprocess_exec(
            *argv, env={**os.environ, **env},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    return launch


async def _amain() -> int:
    # MODE DORMANT (décision utilisateur 2026-07-29) : l'unité systemd est posée dès
    # l'installation, AVANT toute activation — config ou jeton absents ne sont pas une
    # erreur, c'est « pas encore activé depuis le menu admin ». On patiente : dès que le
    # bouton « Activer » a déposé le jeton, le cycle suivant démarre tout seul.
    cfg = None
    warned = False
    while cfg is None:
        try:
            cfg = load_runner_config()
        except RunnerConfigError as exc:
            if not warned:
                logger.info("dormant — %s (activation depuis /admin/connecteurs)", exc)
                warned = True
            await asyncio.sleep(30)
    daemon = MeetingRunnerDaemon(cfg, post=_http_post(cfg.portal_url, cfg.token),
                                 launch=_docker_launcher(cfg),
                                 probe_images=lambda: probe_local_images(cfg.images))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, daemon.stop)
    logger.info("meeting-runner « %s » démarré — portail %s, plateformes %s",
                cfg.runner_name, cfg.portal_url, list(cfg.platforms))
    autoheal = asyncio.ensure_future(_image_autoheal(cfg))
    try:
        await daemon.run_forever()
    finally:
        autoheal.cancel()
    return 0


def main() -> int:
    logging.basicConfig(level=os.environ.get("RUNNER_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return asyncio.run(_amain())


if __name__ == "__main__":
    sys.exit(main())

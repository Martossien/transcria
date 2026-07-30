"""Boucle du meeting-runner — heartbeat, claim, lancement, relais d'états, résultat.

TESTABLE SANS RÉSEAU NI DOCKER : le portail est une fonction injectée `post(path, payload)`
et le lanceur une fabrique `launch(intent)` rendant un processus asyncio. La prod branche
l'HTTP réel et `docker run` (cf. `commands.docker_argv`) ; les tests branchent des fakes.

Cycle (period = poll_interval_s) :
1. heartbeat — annonce capacité/plateformes ; la réponse liste les sessions ANNULÉES encore
   claimées ici → `stop()` des conteneurs concernés (le bot sort proprement, chemin
   « stopped », code 0) ;
2. claim (capacité − actifs) → pour chaque intention : sous-tâche qui lance le bot, relaie
   les lignes `{"bot_event": …}` de sa sortie (BOT_EVENTS=json) vers /v1/meetings/events,
   attend la fin, POST /result avec le code de sortie 0/1/2/3.

Les baux côté serveur (release_expired_leases) couvrent le cas du runner TUÉ net : ses
sessions claimées redeviennent claimables sans rien faire ici — tué/relancé = reprise propre.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from typing import Any

logger = logging.getLogger("connector_service.runner")


class MeetingRunnerDaemon:
    def __init__(self, config, *, post, launch, probe_images=None) -> None:
        self._cfg = config
        self._post = post            # async (path, payload) -> (status, body)
        self._launch = launch        # async (intent) -> process (stdout lisible, .wait())
        self._probe_images = probe_images   # () -> [{name, provider, present}] (Docker réel)
        self._active: dict[str, asyncio.Task] = {}      # session_id → tâche
        self._procs: dict[str, Any] = {}                # session_id → process (pour stop)
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    @property
    def active_count(self) -> int:
        return len(self._active)

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 — un cycle raté ne tue pas le démon
                logger.exception("cycle runner en échec")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._cfg.poll_interval_s)
        # arrêt demandé : laisser finir les bots en cours (jamais de réunion coupée par un
        # simple redéploiement) — le superviseur systemd borne par TimeoutStopSec.
        if self._active:
            await asyncio.gather(*self._active.values(), return_exceptions=True)

    async def run_once(self) -> None:
        status, body = await self._post("/v1/runners/heartbeat", {
            "runner": self._cfg.runner_name, "capacity": self._cfg.capacity,
            "active": self.active_count, "platforms": list(self._cfg.platforms),
            "images": (self._probe_images() if self._probe_images
                       else [{"name": n} for n in self._cfg.images.values()]),
        })
        if status != 200:
            logger.warning("heartbeat refusé (HTTP %s) — jeton runner ? fonctionnalité activée ?", status)
            return
        for session_id in body.get("cancelled_sessions", []):
            await self._stop_session(session_id)

        free = self._cfg.capacity - self.active_count
        if free <= 0:
            return
        status, body = await self._post("/v1/meetings/claim",
                                        {"runner": self._cfg.runner_name, "max": free})
        if status != 200:
            return
        for intent in body.get("sessions", []):
            sid = intent["session_id"]
            task = asyncio.ensure_future(self._run_session(intent))
            self._active[sid] = task

            def _cleanup(_t: asyncio.Task, sid: str = sid) -> None:
                self._active.pop(sid, None)
                self._procs.pop(sid, None)
            task.add_done_callback(_cleanup)

    async def _stop_session(self, session_id: str) -> None:
        proc = self._procs.get(session_id)
        if proc is None:
            return
        logger.info("annulation à chaud : arrêt du bot de la session %s", session_id)
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()             # SIGTERM → le bot sort proprement (« stopped », code 0)

    async def _run_session(self, intent: dict) -> None:
        sid = intent["session_id"]
        try:
            proc = await self._launch(intent)
        except Exception as exc:  # noqa: BLE001 — image absente, docker mort… = config
            logger.exception("lancement du bot impossible (session %s)", sid)
            await self._post(f"/v1/meetings/{sid}/result",
                             {"runner": self._cfg.runner_name, "exit_code": 3,
                              "category": "launch", "message": str(exc)[:200]})
            return
        self._procs[sid] = proc
        await self._post(f"/v1/meetings/{sid}/events",
                         {"runner": self._cfg.runner_name, "event": "joining"})
        relay = asyncio.ensure_future(self._relay_events(sid, proc))
        exit_code = await proc.wait()
        relay.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await relay
        # Les codes 125/126/127 sont ceux de DOCKER lui-même (image absente, démon mort,
        # commande introuvable) — pas du bot : le dire en clair, l'admin lit ce message
        # tel quel sur la carte du job (vécu : « code 125 » cryptique au premier test UI).
        if int(exit_code) in (125, 126, 127):
            category, message = "docker", ("image de bot absente ou démon Docker indisponible "
                                           "— vérifier la check-list /admin/connecteurs")
        elif int(exit_code) == 1:
            # « Non admis » : la cause est presque toujours ACTIONNABLE — on la dit au lieu
            # d'un « code 1 » (revue de complétude 2026-07-30 ; le motif fin
            # — password_required / auth_required / lobby_waiting — reste dans les logs).
            # Le compte d'instance n'est mentionné QUE s'il manque : sur meet.jit.si ou une
            # instance ouverte il n'a pas lieu d'être, et l'évoquer égarerait l'utilisateur.
            message = ("le bot n'a pas été admis — salle protégée par un code d'accès non "
                       "fourni, ou personne ne l'a admis depuis la salle d'attente")
            if not os.environ.get("JITSI_XMPP_USER"):
                message += (" ; si cette instance Jitsi exige de se connecter pour démarrer "
                            "une réunion (auto-hébergée), poser JITSI_XMPP_USER et "
                            "JITSI_XMPP_PASSWORD dans l'environnement du runner")
            category = "bot"
        else:
            category, message = "bot", f"code {exit_code}"
        await self._post(f"/v1/meetings/{sid}/result",
                         {"runner": self._cfg.runner_name, "exit_code": int(exit_code),
                          "category": category, "message": message})

    async def _relay_events(self, session_id: str, proc) -> None:
        """Relaie les lignes `{"bot_event": …}` (BOT_EVENTS=json) — toute autre sortie est
        journalisée telle quelle (les diagnostics du bot restent visibles dans le runner)."""
        stdout = getattr(proc, "stdout", None)
        if stdout is None:
            return
        while True:
            line = await stdout.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip()
            try:
                event = json.loads(text).get("bot_event")
            except (ValueError, AttributeError):
                logger.info("[bot %s] %s", session_id[:8], text)
                continue
            if event:
                await self._post(f"/v1/meetings/{session_id}/events",
                                 {"runner": self._cfg.runner_name, "event": event})

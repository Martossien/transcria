"""Pilote Zoom (client Web) — Playwright, dep opt-in. Gate manuel.

Deuxième plateforme du bot, après Jitsi. L'architecture ne change pas : ce pilote implémente
`BrowserDriver` et le payload de capture (`capture.js`) est réutilisé TEL QUEL — l'interception
WebRTC ne dépend pas de la plateforme, seuls le parcours d'entrée et la lecture d'état en
dépendent. C'est exactement ce que la séparation pilote / capture devait permettre.

Différence importante avec Jitsi : Zoom n'expose AUCUN état applicatif interrogeable. On se
fonde donc sur des signaux de page, toute la décision étant isolée dans `zoom_web_state`
(fonction pure, testée). Conséquence assumée : ce pilote est plus sensible aux évolutions de
l'interface de Zoom que celui de Jitsi.

⚠️ Deux limites à connaître :
- l'identité des locuteurs n'est PAS résolue (Zoom ne publie pas de correspondance
  piste → participant exploitable) : les segments porteront un identifiant de flux, pas un nom ;
- Zoom peut exiger une salle d'attente ou un code : ces cas sont DÉTECTÉS et remontés, mais
  seul l'hôte peut les débloquer.
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from connector_service.bot.browser import CHROMIUM_ARGS
from connector_service.bot.platforms.call_health import CallHealthMonitor
from connector_service.bot.platforms.zoom_web_state import (
    ZoomPhase,
    interpret_zoom_state,
    web_client_url,
)

_CAPTURE_JS = Path(__file__).resolve().parent.parent / "capture.js"

# Sélecteurs relevés sur le DOM réel du client Web. Deux variantes coexistent (interface
# « React » et interface historique) : on cible les deux, Zoom redirigeant selon la réunion.
_NAME_INPUT = '#input-for-name, #inputname, input[placeholder="Your Name" i]'
_JOIN_BUTTON = 'button.preview-join-button, #joinBtn'
_PASSCODE_INPUT = '#input-for-pwd, #inputpasscode, input[type="password"]'
_LEAVE_BUTTON = 'button[aria-label="Leave"]'
# Zoom demande micro et caméra à l'entrée. Ce bouton refuse les deux — c'est exactement ce
# qu'il faut pour un auditeur : il n'émettra rien, sans avoir à se couper ensuite.
_DISMISS_PERMISSIONS = 'button:has-text("Continue without microphone and camera")'

# Instantané de page : ce que la fonction d'interprétation attend. Lecture protégée — la page
# peut être en cours de navigation.
_SNAPSHOT_JS = """() => {
  const safe = (f) => { try { return f(); } catch (e) { return null; } };
  return {
    text: safe(() => (document.body && document.body.innerText) || "") || "",
    title: safe(() => document.title) || "",
    in_meeting: !!safe(() => document.querySelector('button[aria-label="Leave"]')),
    name_input: !!safe(() => document.querySelector(
        '#input-for-name, #inputname, input[placeholder="Your Name" i]')),
    passcode_input: !!safe(() => document.querySelector('#input-for-pwd, #inputpasscode')),
  };
}"""


class ZoomWebDriver:
    """`BrowserDriver` Zoom via le client Web. NON testable en CI (navigateur réel)."""

    def __init__(self, bridge_url: str, *, headless: bool = True, passcode: str = "",
                 alone_poll_s: float = 5.0, alone_timeout_s: float = 30.0,
                 no_media_timeout_s: float = 180.0, ice_timeout_s: float = 30.0,
                 max_duration_s: float = 4 * 3600, admission_poll_s: float = 2.0,
                 prejoin_timeout_ms: int = 25000,
                 capture_options: dict | None = None) -> None:
        self._bridge_url = bridge_url
        self._headless = headless
        # Le code est normalement porté par l'URL (`?pwd=…`) ; ce champ couvre le cas où Zoom
        # le redemande dans un formulaire.
        self._passcode = passcode
        self._alone_poll_s = alone_poll_s
        self._alone_timeout_s = alone_timeout_s
        self._no_media_timeout_s = no_media_timeout_s
        self._ice_timeout_s = ice_timeout_s
        self._max_duration_s = max_duration_s
        self._admission_poll_s = admission_poll_s
        self._prejoin_timeout_ms = prejoin_timeout_ms
        self._capture_options = dict(capture_options or {})
        self._shutting_down = False
        self.admission_reason: str = ""
        self._pw: Any = None
        self._browser: Any = None
        self._page: Any = None

    def set_bridge_url(self, bridge_url: str) -> None:
        """Adresse réelle du pont (port attribué à l'exécution), avant `open()`."""
        self._bridge_url = bridge_url

    async def open(self, meeting_url: str) -> None:
        import json

        from playwright.async_api import async_playwright  # dép opt-in

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self._headless, args=list(CHROMIUM_ARGS))
        self._page = await self._browser.new_page()
        await self._page.add_init_script(
            f"window.__TRANSCRIA_BRIDGE_URL__ = {self._bridge_url!r};\n"
            f"window.__TRANSCRIA_CAPTURE__ = {json.dumps(self._capture_options)};")
        await self._page.add_init_script(path=str(_CAPTURE_JS))
        # Réécriture vers le client Web : le lien d'invitation, lui, pousse à installer
        # l'application de bureau et n'expose aucun média au navigateur.
        await self._page.goto(web_client_url(meeting_url), wait_until="domcontentloaded")

    async def _snapshot(self) -> dict:
        return await self._page.evaluate(_SNAPSHOT_JS)

    async def request_join(self, display_name: str) -> None:
        page = self._page
        # Refuser micro et caméra dès qu'on le propose (le dialogue peut apparaître deux fois).
        for _ in range(2):
            with contextlib.suppress(Exception):
                dismiss = page.locator(_DISMISS_PERMISSIONS)
                if await dismiss.count():
                    await dismiss.first.click(timeout=3000)

        name_box = page.locator(_NAME_INPUT)
        with contextlib.suppress(Exception):
            await name_box.first.wait_for(state="visible", timeout=self._prejoin_timeout_ms)
        if await name_box.count():
            await name_box.first.fill(display_name)

        if self._passcode:
            with contextlib.suppress(Exception):
                passcode_box = page.locator(_PASSCODE_INPUT)
                if await passcode_box.count():
                    await passcode_box.first.fill(self._passcode)

        with contextlib.suppress(Exception):
            join = page.locator(_JOIN_BUTTON)
            if await join.count():
                await join.first.click()

    async def wait_admission(self, timeout_s: float) -> bool:
        """Attend l'entrée effective. Distingue les motifs d'attente des refus définitifs."""
        import asyncio

        waited = 0.0
        self.admission_reason = "timeout"
        while waited < timeout_s:
            try:
                phase = interpret_zoom_state(await self._snapshot())
            except Exception:  # noqa: BLE001 — page pas prête / en navigation
                phase = ZoomPhase.CONNECTING
            if phase is ZoomPhase.ACTIVE:
                self.admission_reason = "admitted"
                return True
            if phase is ZoomPhase.ENDED:
                self.admission_reason = "ended"
                return False
            # Salle d'attente, code requis, hôte absent : on patiente — seul l'hôte peut
            # débloquer, mais la réunion peut s'ouvrir d'un instant à l'autre.
            self.admission_reason = phase.value
            if phase is ZoomPhase.PREJOIN:
                await self.request_join("TranscrIA")     # l'écran a pu réapparaître
            await asyncio.sleep(self._admission_poll_s)
            waited += self._admission_poll_s
        return False

    async def run_until_ended(self) -> str:
        """Surveille la réunion et rend le motif de sortie (mêmes motifs que pour Jitsi)."""
        import asyncio
        import time

        health = CallHealthMonitor(alone_timeout_s=self._alone_timeout_s,
                                   no_media_timeout_s=self._no_media_timeout_s,
                                   ice_timeout_s=self._ice_timeout_s)
        waited = 0.0
        while waited < self._max_duration_s:
            await asyncio.sleep(self._alone_poll_s)
            waited += self._alone_poll_s
            try:
                snapshot = await self._snapshot()
            except Exception:  # noqa: BLE001
                if self._shutting_down:
                    return "stopped"
                return "browser_lost"
            phase = interpret_zoom_state(snapshot)
            if phase is ZoomPhase.ENDED:
                return "conference_ended"
            if phase is not ZoomPhase.ACTIVE:
                return "removed"          # sorti de la réunion sans qu'elle soit annoncée close
            # Zoom ne publie ni comptage ni statistiques exploitables : la santé se limite au
            # média effectivement reçu, mesuré en aval par le pont.
            verdict = health.observe({"membersCount": -1, "iceConnected": True,
                                      "downloadBitrate": 1}, time.monotonic())
            if verdict:
                return verdict
        return "max_duration"

    async def leave(self) -> None:
        with contextlib.suppress(Exception):
            leave = self._page.locator(_LEAVE_BUTTON)
            if await leave.count():
                await leave.first.click()

    async def close(self) -> None:
        self._shutting_down = True
        try:
            if self._browser is not None:
                await self._browser.close()
        finally:
            if self._pw is not None:
                await self._pw.stop()

"""Driver Jitsi (banc d'essai du bot) — Playwright, dep opt-in. Gate manuel.

Jitsi (`meet.jit.si` ou instance auto-hébergée) est public et sans compte → il permet de
valider EN VRAI toute la chaîne du bot (join + capture WebRTC + push sur le pont) en local,
avant de dériver Zoom-web/Teams/Meet (mêmes étapes, autres sélecteurs). NON testable en CI.
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from connector_service.bot.browser import CHROMIUM_ARGS

_CAPTURE_JS = Path(__file__).resolve().parent.parent / "capture.js"


class JitsiDriver:
    """`BrowserDriver` Jitsi. Lance Chromium (Playwright), injecte le payload de capture (avec
    l'URL du pont), rejoint la salle, et suit la présence pour détecter la fin. Le durcissement
    (aloneness fine, reconnexion, sélecteurs multilingues) se règle au gate."""

    def __init__(self, bridge_url: str, *, headless: bool = True, alone_poll_s: float = 5.0,
                 alone_confirmations: int = 3, max_duration_s: float = 4 * 3600,
                 ignore_https_errors: bool = False) -> None:
        self._bridge_url = bridge_url
        self._headless = headless
        # Instance auto-hébergée à certificat auto-signé (bancs d'essai) : accepté au niveau
        # du CONTEXTE Playwright, pas par un flag Chromium global — la confiance TLS reste
        # normale pour les vraies réunions.
        self._ignore_https_errors = ignore_https_errors
        self._alone_poll_s = alone_poll_s
        self._alone_confirmations = alone_confirmations   # polls consécutifs avant de conclure « seul »
        self._max_duration_s = max_duration_s
        self._pw: Any = None
        self._browser: Any = None
        self._page: Any = None

    async def open(self, meeting_url: str) -> None:
        from playwright.async_api import async_playwright  # dép opt-in

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self._headless, args=list(CHROMIUM_ARGS))
        self._page = await self._browser.new_page(
            ignore_https_errors=self._ignore_https_errors)
        # Injecte l'URL du pont puis le payload de capture AVANT chargement de la page.
        await self._page.add_init_script(
            f"window.__TRANSCRIA_BRIDGE_URL__ = {self._bridge_url!r};")
        await self._page.add_init_script(path=str(_CAPTURE_JS))
        await self._page.goto(meeting_url)

    async def request_join(self, display_name: str) -> None:
        page = self._page
        # Attend le rendu (React) du prejoin avant les count() instantanés (toléré absent).
        name_box = page.get_by_placeholder("Enter your name")
        with contextlib.suppress(Exception):
            await name_box.first.wait_for(state="visible", timeout=15000)
        if await name_box.count():
            await name_box.first.fill(display_name)
        # `data-testid` est le sélecteur STABLE de Jitsi (vérifié) ; repli par rôle+texte.
        join = page.locator("[data-testid='prejoin.joinMeeting']")
        if not await join.count():
            join = page.get_by_role("button", name="Join")
        if await join.count():
            await join.first.click()

    async def wait_admission(self, timeout_s: float) -> bool:
        # Salles Jitsi publiques : pas de lobby → on attend que la conférence soit chargée.
        try:
            await self._page.wait_for_selector(
                "#largeVideoContainer, .filmstrip", timeout=timeout_s * 1000)
            return True
        except Exception:  # noqa: BLE001 — timeout = non admis / salle indisponible
            return False

    # Présence : on interroge l'ÉTAT DE L'APPLICATION Jitsi (`APP.conference.membersCount`),
    # pas des classes CSS — vérifié en vrai, et bien plus stable que du scraping DOM (les
    # sélecteurs de filmstrip changent d'une version à l'autre). -1 = état indisponible.
    _MEMBERS_JS = """() => {
      try {
        if (window.APP && APP.conference && typeof APP.conference.membersCount === 'number')
          return APP.conference.membersCount;
      } catch (e) {}
      return -1;
    }"""

    async def run_until_ended(self) -> str:
        import asyncio
        waited = 0.0
        alone_streak = 0
        while waited < self._max_duration_s:
            await asyncio.sleep(self._alone_poll_s)
            waited += self._alone_poll_s
            try:
                members = await self._page.evaluate(self._MEMBERS_JS)
            except Exception:  # noqa: BLE001 — page fermée/crashée
                return "error"
            # membersCount == 1 → il ne reste que nous. -1 (état illisible) : on n'agit PAS,
            # mieux vaut rester que quitter une réunion pleine sur une sonde défaillante.
            if isinstance(members, int) and 0 <= members <= 1:
                alone_streak += 1
                if alone_streak >= self._alone_confirmations:
                    return "left_alone"
            else:
                alone_streak = 0
        return "max_duration"

    async def leave(self) -> None:
        hangup = self._page.get_by_role("button", name="Leave the meeting")
        if await hangup.count():
            await hangup.first.click()

    async def close(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
        finally:
            if self._pw is not None:            # stop() garanti même si close() a levé
                await self._pw.stop()

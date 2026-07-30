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
from connector_service.bot.platforms.call_health import CallHealthMonitor
from connector_service.bot.platforms.jitsi_state import (
    CONFERENCE_STATE_JS,
    KICK_LISTENER_JS,
    ConferencePhase,
    interpret_conference_state,
)

_CAPTURE_JS = Path(__file__).resolve().parent.parent / "capture.js"
# Résolveur d'identité SPÉCIFIQUE à Jitsi : traduit une piste WebRTC en participant nommé,
# en interrogeant l'état de l'application. `capture.js` reste générique.
_IDENTITY_JS = Path(__file__).resolve().parent / "jitsi_identity.js"

# Config de join du bot AUDITEUR — transmise par le fragment de l'URL (Jitsi la lit là).
# Chaque option évite un problème concret :
#  - `disableInitialGUM` : ne demande JAMAIS micro/caméra. Sans elle, le périphérique
#    factice du navigateur diffuse sa tonalité de test dans la réunion (bip entendu par les
#    participants) ; c'est plus sûr que de simplement se couper après coup.
#  - `startWithAudioMuted`/`startWithVideoMuted` : ceinture et bretelles, le bot n'émet rien.
#  - `prejoinConfig.enabled=false` : supprime l'écran d'accueil, donc TOUS les sélecteurs de
#    saisie de nom et de bouton « Join » — un DOM en moins à suivre à chaque version.
#  - `deeplinking.disabled=true` : pas d'interstitiel « ouvrir dans l'application ».
#  - `p2p.enabled=false` : force le passage par le SFU. En tête-à-tête, le mode pair-à-pair
#    change la topologie des flux et rend la capture imprévisible.
#  - `requireDisplayName=false` : un bot sans nom ne doit pas être bloqué.
# ⚠ NE PAS ajouter `config.startSilent` : il coupe aussi la RÉCEPTION audio, or c'est
# précisément ce qu'on vient capturer.
_SILENT_JOIN_CONFIG = (
    "config.disableInitialGUM=true"
    "&config.startWithAudioMuted=true"
    "&config.startWithVideoMuted=true"
    "&config.prejoinConfig.enabled=false"
    "&config.deeplinking.disabled=true"
    "&config.p2p.enabled=false"
    "&config.requireDisplayName=false"
)


def _muted_url(meeting_url: str) -> str:
    """Ajoute la config « micro et caméra coupés » au fragment de l'URL de réunion."""
    if "#" in meeting_url:
        return f"{meeting_url}&{_SILENT_JOIN_CONFIG}"
    return f"{meeting_url}#{_SILENT_JOIN_CONFIG}"


def _join_url(base: str, display_name: str) -> str:
    """URL du rechargement « pose le nom » : config muette ET nom, dans le MÊME fragment.

    Le fragment est un seul espace de paramètres liés par `&` (syntaxe documentée du
    handbook, même canal que Jibri, l'enregistreur officiel de Jitsi). Recharger avec le nom
    SEUL avait écrasé toute la config muette — bip du périphérique factice et mire verte
    diffusés aux participants, p2p réactivé (régression vécue au gate du 2026-07-30).
    """
    from urllib.parse import quote

    return f'{base}#{_SILENT_JOIN_CONFIG}&userInfo.displayName="{quote(display_name)}"'


class JitsiDriver:
    """`BrowserDriver` Jitsi. Lance Chromium (Playwright), injecte le payload de capture (avec
    l'URL du pont), rejoint la salle, et suit la présence pour détecter la fin. Le durcissement
    (aloneness fine, reconnexion, sélecteurs multilingues) se règle au gate."""

    def __init__(self, bridge_url: str, *, headless: bool = True, alone_poll_s: float = 5.0,
                 alone_timeout_s: float = 30.0, no_media_timeout_s: float = 180.0,
                 ice_timeout_s: float = 30.0, max_duration_s: float = 4 * 3600,
                 ignore_https_errors: bool = False, prejoin_timeout_ms: int = 15000,
                 admission_poll_s: float = 1.0,
                 capture_options: dict | None = None) -> None:
        self._bridge_url = bridge_url
        self._headless = headless
        # Instance auto-hébergée à certificat auto-signé (bancs d'essai) : accepté au niveau
        # du CONTEXTE Playwright, pas par un flag Chromium global — la confiance TLS reste
        # normale pour les vraies réunions.
        self._ignore_https_errors = ignore_https_errors
        self._alone_poll_s = alone_poll_s
        self._alone_timeout_s = alone_timeout_s   # durée SEUL avant de quitter
        self._no_media_timeout_s = no_media_timeout_s
        self._ice_timeout_s = ice_timeout_s
        # Vrai pendant un arrêt volontaire : évite de qualifier de panne un
        # navigateur qu'on est justement en train de fermer.
        self._shutting_down = False
        self._max_duration_s = max_duration_s
        self._prejoin_timeout_ms = prejoin_timeout_ms
        self._admission_poll_s = admission_poll_s
        # Motif de la dernière décision d'admission (admitted / lobby_waiting /
        # password_required / auth_required / timeout) — remonté pour diagnostic.
        self.admission_reason: str = ""
        # Réglages de capture transmis au payload (seuil de voix, délais d'identité…) :
        # ils dépendent du terrain, ils ne doivent pas vivre en dur dans le JS.
        self._capture_options = dict(capture_options or {})
        self._pw: Any = None
        self._browser: Any = None
        self._page: Any = None

    def set_bridge_url(self, bridge_url: str) -> None:
        """Adresse RÉELLE du pont, connue seulement après ouverture du serveur (port auto).
        Appelée avant `open()` — le payload de capture est injecté avec cette URL."""
        self._bridge_url = bridge_url

    async def open(self, meeting_url: str) -> None:
        from playwright.async_api import async_playwright  # dép opt-in

        # Nom de salle retenu pour distinguer « salle close » (l'URL a navigué ailleurs)
        # d'une vraie perte de navigateur (cf. run_until_ended).
        self._room_name = meeting_url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self._headless, args=list(CHROMIUM_ARGS))
        self._page = await self._browser.new_page(
            ignore_https_errors=self._ignore_https_errors)
        # Injecte l'URL du pont puis le payload de capture AVANT chargement de la page.
        import json as _json
        await self._page.add_init_script(
            f"window.__TRANSCRIA_BRIDGE_URL__ = {self._bridge_url!r};\n"
            f"window.__TRANSCRIA_CAPTURE__ = {_json.dumps(self._capture_options)};")
        await self._page.add_init_script(path=str(_IDENTITY_JS))
        await self._page.add_init_script(path=str(_CAPTURE_JS))
        await self._page.goto(_muted_url(meeting_url))

    async def request_join(self, display_name: str) -> None:
        page = self._page
        # CEINTURE + BRETELLES pour le nom affiché (vécu : le bot apparaissait « Fellow
        # Jitster », le nom d'invité aléatoire de Jitsi — le champ prejoin n'avait pas pris).
        # 1) l'API de config par URL est le canal FIABLE de Jitsi : recharger la page avec
        #    userInfo.displayName="…" pose le nom quel que soit l'état du prejoin ;
        # 2) le champ prejoin reste rempli en repli (instances au fragment désactivé).
        # ⚠ Le rechargement passe par `_join_url` : config muette ET nom dans le même
        # fragment (le nom seul écraserait la config — cf. docstring de `_join_url`).
        if display_name:
            with contextlib.suppress(Exception):
                base = (page.url or "").split("#")[0]
                await page.goto(_join_url(base, display_name))
        # Attend le rendu (React) du prejoin avant les count() instantanés (toléré absent).
        name_box = page.get_by_placeholder("Enter your name")
        with contextlib.suppress(Exception):
            await name_box.first.wait_for(state="visible",
                                          timeout=self._prejoin_timeout_ms)
        if await name_box.count():
            await name_box.first.fill(display_name)
        # `data-testid` est le sélecteur STABLE de Jitsi (vérifié) ; repli par rôle+texte.
        join = page.locator("[data-testid='prejoin.joinMeeting']")
        if not await join.count():
            join = page.get_by_role("button", name="Join")
        if await join.count():
            await join.first.click()

    async def _phase(self) -> ConferencePhase:
        """Phase courante d'après l'ÉTAT de l'application (pas un sélecteur d'écran)."""
        return interpret_conference_state(await self._page.evaluate(CONFERENCE_STATE_JS))

    async def wait_admission(self, timeout_s: float) -> bool:
        """Attend l'entrée effective en conférence.

        Gère la SALLE D'ATTENTE : tant qu'un modérateur n'a pas admis le bot, la phase reste
        `lobby_waiting` — on patiente jusqu'au délai imparti au lieu de conclure trop tôt.
        Un mot de passe ou une authentification requis sont des refus DÉFINITIFS : inutile
        d'attendre, on rend la main immédiatement avec un motif exploitable.
        """
        import asyncio
        waited = 0.0
        self.admission_reason = "timeout"
        while waited < timeout_s:
            try:
                phase = await self._phase()
            except Exception:  # noqa: BLE001 — page pas prête / fermée
                phase = ConferencePhase.CONNECTING
            if phase is ConferencePhase.ACTIVE:
                self.admission_reason = "admitted"
                # L'écouteur d'expulsion ne peut s'accrocher qu'une fois la conférence
                # établie (il s'abonne à la salle XMPP).
                with contextlib.suppress(Exception):
                    await self._page.evaluate(KICK_LISTENER_JS)
                return True
            if phase in (ConferencePhase.PASSWORD_REQUIRED, ConferencePhase.AUTH_REQUIRED,
                         ConferencePhase.KICKED, ConferencePhase.ENDED):
                self.admission_reason = phase.value      # refus définitif : on n'insiste pas
                return False
            if phase is ConferencePhase.LOBBY_WAITING:
                self.admission_reason = "lobby_waiting"  # on patiente, c'est normal
            await asyncio.sleep(self._admission_poll_s)
            waited += self._admission_poll_s
        return False

    async def run_until_ended(self) -> str:
        """Surveille la réunion et rend le MOTIF de sortie.

        Motifs de succès : `left_alone`, `removed`, `conference_ended`, `max_duration`.
        Motifs d'échec : `no_media`, `ice_failed`, `browser_lost` — la distinction compte,
        elle décide si la session est rejouable.
        """
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
                raw = await self._page.evaluate(CONFERENCE_STATE_JS)
            except Exception:  # noqa: BLE001
                # Toute erreur d'exécution ici (script gelé, contact perdu, page morte) veut
                # dire la même chose : le navigateur ne répond plus. On ne cherche pas à
                # distinguer les causes — sauf pendant un arrêt volontaire, où c'est normal.
                if self._shutting_down:
                    return "stopped"
                # La fermeture de la salle par le modérateur NAVIGUE la page (l'évaluation
                # lève alors) : si l'URL a quitté la salle, c'est une fin de réunion, pas
                # une panne de navigateur (vécu : classé error → rejeu dans une salle vide).
                try:
                    if self._room_name and self._room_name not in (self._page.url or ""):
                        return "conference_ended"
                except Exception:  # noqa: BLE001
                    pass
                return "browser_lost"

            phase = interpret_conference_state(raw)
            # Un modérateur peut sortir le bot, ou clore la réunion : sans ces deux sorties,
            # le bot tournerait dans le vide jusqu'à sa durée maximale.
            if phase is ConferencePhase.KICKED:
                return "removed"
            if phase is ConferencePhase.ENDED:
                return "conference_ended"

            verdict = health.observe(raw, time.monotonic())
            if verdict:
                return verdict
        return "max_duration"

    async def leave(self) -> None:
        hangup = self._page.get_by_role("button", name="Leave the meeting")
        if await hangup.count():
            await hangup.first.click()

    async def close(self) -> None:
        self._shutting_down = True
        try:
            if self._browser is not None:
                await self._browser.close()
        finally:
            if self._pw is not None:            # stop() garanti même si close() a levé
                await self._pw.stop()

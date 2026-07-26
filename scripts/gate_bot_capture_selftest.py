#!/usr/bin/env python
"""AUTO-TEST de la chaîne de capture du bot — SANS réunion, SANS humain.

Rejoue tout ce que fait le bot, sauf le DOM de la plateforme : vrai Chromium (mêmes options
que le driver), vrai `capture.js` injecté, boucle WebRTC synthétique dans la page (un
oscillateur envoyé d'une RTCPeerConnection à une autre), vrai serveur de pont, et décodage
Python jusqu'au `RawFrame`.

Il vérifie les points qui ont RÉELLEMENT cassé lors de la mise au point :
  1. WebCodecs (`MediaStreamTrackProcessor`) disponible dans ce Chromium ;
  2. l'interception de `RTCPeerConnection` est bien installée ;
  3. la piste audio DISTANTE coule (elle exige un puits `<audio>` — sans lui, zéro frame) ;
  4. la WebSocket vers 127.0.0.1 n'est pas bloquée depuis une page publique
     (Local Network Access — bloquant par défaut, d'où les flags du driver) ;
  5. le PCM arrive côté Python et se décode en `RawFrame`.

À relancer après toute modification de `capture.js` ou des options du navigateur.

Usage :  python scripts/gate_bot_capture_selftest.py [--origin https://example.com]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connector_service.bot.browser import CHROMIUM_ARGS  # noqa: E402
from connector_service.live.bridge_source import parse_bridge_message  # noqa: E402

CAPTURE_JS = Path(__file__).resolve().parent.parent / "connector_service" / "bot" / "capture.js"
TARGET_FRAMES = 5

# Boucle WebRTC en page : pc1 (oscillateur) → pc2. L'interception de capture.js doit se
# déclencher sur pc2 et pousser le PCM sur le pont.
LOOPBACK_JS = """
async () => {
  const out = {};
  out.webcodecs = "MediaStreamTrackProcessor" in window;
  out.intercepted = window.RTCPeerConnection.name !== "RTCPeerConnection";
  const ctx = new AudioContext(); await ctx.resume();
  const osc = ctx.createOscillator();
  const dst = ctx.createMediaStreamDestination();
  osc.connect(dst); osc.start();
  const pc1 = new RTCPeerConnection(), pc2 = new RTCPeerConnection();
  pc1.onicecandidate = e => { if (e.candidate) pc2.addIceCandidate(e.candidate); };
  pc2.onicecandidate = e => { if (e.candidate) pc1.addIceCandidate(e.candidate); };
  pc1.addTrack(dst.stream.getAudioTracks()[0], dst.stream);
  const offer = await pc1.createOffer(); await pc1.setLocalDescription(offer);
  await pc2.setRemoteDescription(offer);
  const answer = await pc2.createAnswer(); await pc2.setLocalDescription(answer);
  await pc1.setRemoteDescription(answer);
  await new Promise(r => setTimeout(r, 2000));
  out.rtcState = pc2.connectionState;
  return out;
}
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def main() -> int:
    parser = argparse.ArgumentParser(description="Auto-test de la capture du bot")
    parser.add_argument("--origin", default="https://example.com",
                        help="page publique HTTPS de test (repli local si pas d'internet)")
    parser.add_argument("--show", action="store_true", help="fenêtre visible")
    args = parser.parse_args()

    import websockets
    from playwright.async_api import async_playwright

    ws_port = _free_port()
    received: list[str] = []
    enough = asyncio.Event()

    async def handler(conn):
        async for raw in conn:
            received.append(raw)
            if len(received) >= TARGET_FRAMES:
                enough.set()

    server = await websockets.serve(handler, "127.0.0.1", ws_port)
    checks: dict[str, bool] = {}

    with TemporaryDirectory() as tmp:
        (Path(tmp) / "index.html").write_text("<!doctype html><title>selftest</title>")
        http_port = _free_port()
        httpd = ThreadingHTTPServer(
            ("127.0.0.1", http_port),
            partial(SimpleHTTPRequestHandler, directory=tmp))
        httpd.log_message = lambda *a, **k: None  # type: ignore[method-assign]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=not args.show,
                                               args=list(CHROMIUM_ARGS))
            page = await browser.new_page()
            blocked: list[str] = []
            page.on("console", lambda m: blocked.append(m.text)
                    if m.type == "error" and "WebSocket" in m.text else None)
            await page.add_init_script(
                f"window.__TRANSCRIA_BRIDGE_URL__ = 'ws://127.0.0.1:{ws_port}';")
            await page.add_init_script(path=str(CAPTURE_JS))

            origin = args.origin
            try:                                   # page PUBLIQUE = conditions réelles (LNA)
                await page.goto(origin, timeout=20000)
            except Exception:                      # noqa: BLE001 — repli hors-ligne
                origin = f"http://127.0.0.1:{http_port}/"
                await page.goto(origin)
            print(f"page de test : {origin}")

            page_state = await page.evaluate(LOOPBACK_JS)
            checks["WebCodecs disponible"] = bool(page_state.get("webcodecs"))
            checks["interception RTCPeerConnection"] = bool(page_state.get("intercepted"))
            checks["WebRTC connecté"] = page_state.get("rtcState") == "connected"

            with __import__("contextlib").suppress(asyncio.TimeoutError):
                await asyncio.wait_for(enough.wait(), timeout=10)
            checks["WebSocket vers 127.0.0.1 non bloquée"] = not blocked
            await browser.close()
        httpd.shutdown()

    server.close()
    await server.wait_closed()

    checks[f"≥{TARGET_FRAMES} frames PCM reçues"] = len(received) >= TARGET_FRAMES
    decoded = parse_bridge_message(json.loads(received[0])) if received else None
    checks["PCM décodable en RawFrame"] = decoded is not None

    print("\n────────── AUTO-TEST DE LA CAPTURE ──────────")
    for label, ok in checks.items():
        print(f"  [{'OK ' if ok else 'ÉCHEC'}] {label}")
    print(f"\nframes reçues : {len(received)}")
    if decoded:
        pid, payload, rate, chans, _name, _ts = decoded
        print(f"1re frame     : participant={pid!r} {len(payload)} octets "
              f"{rate} Hz {chans} canal/aux")
    if all(checks.values()):
        print("\n✅ Chaîne de capture VALIDÉE (hors DOM de la plateforme).")
        return 0
    print("\n❌ Chaîne de capture CASSÉE — corriger avant tout gate en réunion.")
    if not received and blocked:
        print(f"   indice : {blocked[0][:140]}")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

"""Options de lancement du navigateur du bot — PARTAGÉES driver ⟂ auto-test.

Ces options ne sont pas cosmétiques : chacune corrige un blocage constaté empiriquement
(cf. `scripts/gate_bot_capture_selftest.py`, qui rejoue la chaîne complète). Elles vivent
ici pour que l'auto-test valide EXACTEMENT la configuration utilisée en vrai.
"""
from __future__ import annotations

CHROMIUM_ARGS: tuple[str, ...] = (
    # Accorde micro/caméra sans interaction (le bot ne peut pas cliquer une pastille).
    "--use-fake-ui-for-media-stream",
    # Serveur sans carte son : sans périphérique factice, getUserMedia échoue et le client
    # de réunion refuse souvent de rejoindre.
    "--use-fake-device-for-media-stream",
    # VITAL : sans ça, Chromium BLOQUE la WebSocket de capture.js vers 127.0.0.1 depuis une
    # page publique — net::ERR_BLOCKED_BY_LOCAL_NETWORK_ACCESS_CHECKS → aucun PCM ne remonte.
    "--disable-features=LocalNetworkAccessChecks,PrivateNetworkAccessChecks",
    # Laisse démarrer le puits audio (<audio> muet) sans geste utilisateur.
    "--autoplay-policy=no-user-gesture-required",
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
)

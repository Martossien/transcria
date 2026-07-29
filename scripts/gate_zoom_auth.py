#!/usr/bin/env python3
"""Vérifie que des identifiants Zoom sont ACCEPTÉS — sans rejoindre la moindre réunion.

POURQUOI CE SCRIPT EXISTE. Le catalogue des connecteurs annonce Zoom comme `testable`, au
motif qu'« un vrai appel d'authentification est possible ». C'était vrai en théorie et faux en
pratique : le seul gate existant (`gate_bot_zoom_sdk.py`) exige `--meeting`, donc une réunion
ouverte et quelqu'un pour l'ouvrir. Après avoir créé l'application sur le Marketplace, on
voulait savoir en dix secondes si elle fonctionne — pas organiser une réunion.

CE QUE LE TEST PROUVE, ET CE QU'IL NE PROUVE PAS. L'authentification du SDK atteste de
l'APPLICATION, pas de la réunion : le numéro passé dans la signature n'est pas validé. Un
succès signifie donc « Client ID/Secret bons ET Meeting SDK activé ». Un échec renvoie
`AUTHRET_JWTTOKENWRONG`, code que Zoom utilise INDIFFÉREMMENT pour un secret erroné et pour un
SDK désactivé — le message d'erreur énumère donc les causes dans leur ordre de probabilité,
faute de pouvoir les distinguer.

CE SCRIPT NE TOURNE QUE DANS L'IMAGE `transcria-zoom-sdk` : le Meeting SDK n'est pas une
bibliothèque réseau mais un client Zoom complet, qui exige un bus D-Bus et un sous-système
audio. Sans eux il ne renvoie pas d'erreur — il plante par segfault (cf.
`docker/zoom_sdk_entrypoint.sh`).

    docker run --rm --network host \
      -e ZOOM_CLIENT_ID=… -e ZOOM_CLIENT_SECRET=… \
      --entrypoint /usr/local/bin/zoom-sdk-entrypoint \
      transcria-zoom-sdk:latest python3 -u /app/scripts/gate_zoom_auth.py

⚠ Le Client Secret se lit UNIQUEMENT dans l'environnement : lui donner une option de ligne de
commande le rendrait lisible dans la liste des processus de la machine.

Codes de sortie : 0 accepté · 1 refusé · 2 SDK indisponible · 3 refus immédiat · 4 silence.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Lancé par CHEMIN (`python3 /app/scripts/…`), `sys.path[0]` vaut `scripts/` et non la racine :
# sans cette ligne, `connector_service` est introuvable. Même motif que gate_bot_zoom_sdk.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connector_service.live.glib_loop import GLibPump  # noqa: E402
from connector_service.live.zoom_sdk_state import describe_auth_result  # noqa: E402
from connector_service.signatures import (  # noqa: E402
    ROLE_PARTICIPANT,
    zoom_meeting_sdk_signature,
)

# Numéro FICTIF. La signature doit en porter un, mais l'authentification ne le valide pas :
# y mettre une vraie réunion n'apporterait rien et la publierait dans les journaux.
NUMERO_FICTIF = "1234567890"
DEFAULT_TIMEOUT_S = 40.0


async def verifier(client_id: str, client_secret: str, *, timeout_s: float,
                   web_domain: str) -> int:
    try:
        import zoom_meeting_sdk as zoom  # noqa: I001 — dép opt-in, présente dans l'image dédiée
    except ImportError:
        print("zoom_meeting_sdk introuvable — lancer ce script DANS l'image "
              "transcria-zoom-sdk (cf. en-tête).", file=sys.stderr)
        return 2

    loop = asyncio.get_running_loop()
    # Le SDK conserve des pointeurs BRUTS sur les objets de rappel : les laisser au ramasse-
    # miettes provoque un segfault peu après l'authentification. Cause du premier plantage
    # rencontré sur ce chemin, d'où cette liste.
    retenus: list[object] = []

    init = zoom.InitParam()
    init.strWebDomain = web_domain
    init.strSupportUrl = web_domain
    init.strBrandingName = "TranscrIA (vérification)"
    init.emLanguageID = zoom.SDK_LANGUAGE_ID.LANGUAGE_English
    init.enableLogByDefault = True
    init.uiLogFileSize = 5
    err = zoom.InitSDK(init)
    if err != zoom.SDKError.SDKERR_SUCCESS:
        print(f"RÉSULTAT   : InitSDK a échoué → {err}")
        return 2

    stop = asyncio.Event()
    pompe = asyncio.ensure_future(GLibPump().run(stop))
    try:
        service = zoom.CreateAuthService()
        retenus.append(service)
        issue: dict[str, object] = {}
        fini = asyncio.Event()

        def _retour(resultat: object) -> None:
            nom = getattr(resultat, "name", str(resultat))
            issue["code"] = nom
            issue["diagnostic"] = describe_auth_result(nom)
            loop.call_soon_threadsafe(fini.set)

        rappels = zoom.AuthServiceEventCallbacks(onAuthenticationReturnCallback=_retour)
        retenus.append(rappels)
        service.SetEvent(rappels)

        contexte = zoom.AuthContext()
        contexte.jwt_token = zoom_meeting_sdk_signature(
            client_id, client_secret, NUMERO_FICTIF, role=ROLE_PARTICIPANT)
        err = service.SDKAuth(contexte)
        if err != zoom.SDKError.SDKERR_SUCCESS:
            print(f"RÉSULTAT   : SDKAuth refusé immédiatement → {err}")
            return 3

        try:
            await asyncio.wait_for(fini.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            print(f"RÉSULTAT   : aucune réponse en {timeout_s:.0f} s — ni acceptation, "
                  f"ni refus. Vérifier l'accès réseau sortant vers Zoom.")
            return 4

        diagnostic = issue["diagnostic"]
        print(f"CODE ZOOM  : {issue['code']}")
        print(f"DIAGNOSTIC : {diagnostic.message}")
        print(f"RÉSULTAT   : {'ACCEPTÉ' if diagnostic.ok else 'REFUSÉ'}")
        return 0 if diagnostic.ok else 1
    finally:
        stop.set()
        pompe.cancel()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--client-id", default=os.environ.get("ZOOM_CLIENT_ID", ""))
    parser.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--web-domain", default="https://zoom.us")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    client_secret = os.environ.get("ZOOM_CLIENT_SECRET", "")
    if not args.client_id or not client_secret:
        print("ZOOM_CLIENT_ID et ZOOM_CLIENT_SECRET sont requis (variables d'environnement ; "
              "le secret n'a volontairement pas d'option de ligne de commande).",
              file=sys.stderr)
        return 2
    return asyncio.run(verifier(args.client_id, client_secret,
                                timeout_s=args.timeout_s, web_domain=args.web_domain))


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    # Sortie IMMÉDIATE : démonté par le ramasse-miettes de Python, le SDK plante au nettoyage.
    # Déjà rencontré deux fois sur ce chemin — on ne laisse pas l'interpréteur défaire.
    os._exit(code)

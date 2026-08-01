"""Point d'entrée du service Meet : `python -m connector_service.meet_main`.

Fait UNIQUEMENT deux choses — lire les identités là où l'administrateur les a mises, et
lancer la boucle. Toute la logique vit dans `meet_service.py`, testée sans réseau.

POURQUOI IL LIT LA CONFIG À CHAQUE TOUR. L'administrateur renseigne la fiche Meet dans
l'interface, souvent APRÈS que l'unité systemd a été posée. La lire une fois au démarrage
obligerait à redémarrer le service pour qu'il en tienne compte — exactement le genre de
commande que « l'admin ne touche que l'interface » cherche à supprimer.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from connector_service.meet_service import MeetNotConfigured, MeetService, MeetServiceConfig

logger = logging.getLogger("connector_service.meet")

TOKEN_FILENAME = "meeting_runner_token.txt"


def read_identities(repo_root: Path) -> dict[str, str]:
    """Identités de la fiche Meet, environnement en repli — même ordre que le portail.

    La lecture passe par le FICHIER de configuration et non par `ConfigService.get_singleton`
    (qui mettrait en cache) : ce service tourne des semaines, la fiche peut changer entre
    deux tours.
    """
    identites: dict[str, str] = {}
    try:
        import yaml

        from transcria.services.config_service import ConfigService
        cfg = yaml.safe_load(Path(ConfigService.get_path()).read_text(encoding="utf-8")) or {}
        penv = ((cfg.get("connectors") or {}).get("meetings") or {}).get("platform_env") or {}
        identites.update({str(k): str(v) for k, v in penv.items()})
    except Exception as exc:  # noqa: BLE001 — hors portail : environnement seul
        logger.debug("configuration du portail illisible (%s) — environnement seul", exc)
    for cle in ("MEET_SERVICE_ACCOUNT_JSON", "MEET_IMPERSONATE_USER", "MEET_PUBSUB_SUBSCRIPTION"):
        if not identites.get(cle) and os.environ.get(cle):
            identites[cle] = os.environ[cle]
    return identites


def read_portal(repo_root: Path) -> tuple[str, str]:
    """(URL du portail, jeton d'exécutant) — celui que le bouton « Activer » dépose."""
    base = os.environ.get("TRANSCRIA_BASE_URL", "http://127.0.0.1:7870").rstrip("/")
    jeton = repo_root / "instance" / TOKEN_FILENAME
    try:
        return base, jeton.read_text(encoding="utf-8").strip()
    except OSError:
        return base, ""


def read_wanted_spaces() -> tuple[str, ...]:
    """Réunions que l'administrateur a demandé de surveiller (fichier de configuration).

    Relue à CHAQUE tour, comme les identités : l'admin ajoute une réunion dans l'interface
    et le service doit s'y conformer sans qu'on le redémarre.
    """
    try:
        import yaml

        from transcria.services.config_service import ConfigService
        cfg = yaml.safe_load(Path(ConfigService.get_path()).read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 — exécutant distant : rien à surveiller de ce côté
        return ()
    reunions = ((cfg.get("connectors") or {}).get("meetings") or {}).get("meet_spaces") or []
    return tuple(str(r).strip() for r in reunions if str(r).strip())


def read_watched_users(portal_url: str, token: str) -> tuple[str, ...]:
    """Utilisateurs à surveiller, demandés AU PORTAIL.

    Par HTTP et non par un fichier : le service Meet peut vivre sur une autre machine. Un
    échec n'est pas fatal — on garde ce qu'on avait, plutôt que de tout désabonner parce que
    le portail redémarrait au mauvais moment.
    """
    import json as _json
    import urllib.error
    import urllib.request

    requete = urllib.request.Request(f"{portal_url}/v1/meetings/meet/watched-users",
                                     headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(requete, timeout=15) as reponse:
            charge = _json.loads(reponse.read().decode() or "{}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.warning("[meet] liste des utilisateurs surveillés indisponible (%s) — "
                       "abonnements inchangés ce tour", exc.__class__.__name__)
        return ()
    return tuple(str(a) for a in (charge.get("users") or []) if str(a).strip())


def load_config(repo_root: Path) -> MeetServiceConfig:
    base, jeton = read_portal(repo_root)
    return MeetServiceConfig.from_identities(
        read_identities(repo_root), portal_url=base, portal_token=jeton,
        wanted_spaces=read_wanted_spaces(),
        watched_users=read_watched_users(base, jeton),
        instance_path=str(repo_root / "instance"))


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get("TRANSCRIA_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    racine = Path(os.environ.get("TRANSCRIA_REPO_ROOT")
                  or Path(__file__).resolve().parent.parent)
    service = MeetService(lambda: load_config(racine))
    logger.info("service Meet démarré (racine=%s)", racine)
    try:
        asyncio.run(service.run_forever())
    except KeyboardInterrupt:
        logger.info("arrêt demandé")
    except MeetNotConfigured as exc:      # ne devrait pas remonter : la boucle l'absorbe
        logger.error("configuration Meet : %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

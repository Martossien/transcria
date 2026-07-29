"""Auto-provisionnement du meeting-runner LOCAL — l'interrupteur admin fait tout (décision
utilisateur 2026-07-29 : « l'admin ne touche que l'interface »).

À l'activation depuis /admin/connecteurs, le portail : (1) crée le compte de SERVICE
`svc-runner` s'il manque (mot de passe aléatoire jamais montré — ce compte ne se connecte
pas, il porte un jeton) ; (2) émet un jeton d'API et l'écrit dans
`instance/meeting_runner_token.txt` (0600) — le chemin que le runner dormant lit ; (3) active
`connectors.meetings.enabled` + `live.facade.enabled` (dépendance interne masquée à l'admin)
et inscrit le compte dans `runner_usernames`, via la MÊME voie validée que le formulaire de
config (`ConfigService.save_if_valid`). Idempotent : un jeton-fichier présent n'est jamais
remplacé (le runner local le lit peut-être déjà) — la rotation passe par la révocation.

⚠ Volet couvert par la revue sécurité (avec meeting_ref_crypto) : dépôt d'un secret sur
disque local par le portail — acté avec l'utilisateur, à ratifier.

La check-list (`meetings_checklist`) rend chaque prérequis avec son verdict ET son remède en
une phrase — « l'admin voit facilement si c'est bon ».
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from flask import current_app
from flask_babel import lazy_gettext as _l

from transcria.auth.api_tokens import create_token, revoke_token
from transcria.auth.models import ApiToken, Role
from transcria.auth.store import UserStore
from transcria.config import _deep_merge
from transcria.database import db
from transcria.ingestion.session_models import MeetingRunner
from transcria.ingestion.session_store import MeetingSessionStore
from transcria.services.config_service import ConfigService

RUNNER_ACCOUNT = "svc-runner"
TOKEN_FILENAME = "meeting_runner_token.txt"


def _token_path() -> Path:

    return Path(current_app.instance_path) / TOKEN_FILENAME


def provision_local_runner(cfg: dict, config_path: str) -> tuple[bool, list[str]]:
    """Active la fonctionnalité et provisionne le runner local. Rend (ok, messages)."""

    messages: list[str] = []

    user = UserStore.get_by_username(RUNNER_ACCOUNT)
    if user is None:
        user = UserStore.create_user(RUNNER_ACCOUNT, secrets.token_urlsafe(24), role=Role.OPERATOR)
        messages.append(str(_l("Compte de service créé : %(name)s", name=RUNNER_ACCOUNT)))

    token_file = _token_path()
    if not token_file.exists():
        full, _record = create_token(user.id, label="meeting-runner (auto-provisionné)")
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(full + "\n", encoding="utf-8")
        token_file.chmod(0o600)
        messages.append(str(_l("Jeton d'exécutant déposé : %(path)s", path=str(token_file))))

    meetings = ((cfg.get("connectors") or {}).get("meetings") or {})
    usernames = list(meetings.get("runner_usernames") or [])
    if RUNNER_ACCOUNT not in usernames:
        usernames.append(RUNNER_ACCOUNT)
    partial = {
        "connectors": {"meetings": {"enabled": True, "runner_usernames": usernames}},
        "live": {"facade": {"enabled": True}},
    }
    merged = _deep_merge(cfg, partial)
    ok, errors, _warnings = ConfigService.save_if_valid(merged, config_path)
    if not ok:
        return False, errors
    messages.append(str(_l("Réunions en ligne activées (façade temps réel incluse).")))
    return True, messages


def disable_meetings(cfg: dict, config_path: str) -> tuple[bool, list[str]]:
    """Coupe la fonctionnalité (la façade reste — d'autres usages peuvent en dépendre)."""

    merged = _deep_merge(cfg, {"connectors": {"meetings": {"enabled": False}}})
    ok, errors, _warnings = ConfigService.save_if_valid(merged, config_path)
    return (True, [str(_l("Réunions en ligne désactivées."))]) if ok else (False, errors)


def revoke_runner(name: str) -> tuple[bool, str]:
    """Révoque PRÉCISÉMENT le jeton utilisé par cet exécutant (token_id du heartbeat) et
    oublie son annonce. Son prochain battement sera refusé (401) — il s'arrête de lui-même."""

    runner = db.session.get(MeetingRunner, name)
    if runner is None:
        return False, "exécutant inconnu"
    if runner.token_id:

        token = db.session.execute(
            db.select(ApiToken).where(ApiToken.token_id == runner.token_id)
        ).scalar_one_or_none()
        if token is not None:
            revoke_token(token)
    # Si c'est le jeton local auto-provisionné, retirer aussi le fichier : une réactivation
    # future en générera un neuf (rotation propre).
    token_file = _token_path()
    if token_file.exists() and runner.token_id and runner.token_id in token_file.read_text(encoding="utf-8"):
        token_file.unlink()
    db.session.delete(runner)
    db.session.commit()
    return True, ""


def meetings_checklist(cfg: dict) -> list[dict]:
    """La check-list vivante de /admin/connecteurs : verdict + REMÈDE par prérequis."""

    meetings = ((cfg.get("connectors") or {}).get("meetings") or {})
    enabled = bool(meetings.get("enabled", False))
    facade_on = bool(((cfg.get("live") or {}).get("facade") or {}).get("enabled", False))
    key_present = bool((os.environ.get("TRANSCRIA_MEETING_REF_KEY") or "").strip())
    account = UserStore.get_by_username(RUNNER_ACCOUNT)
    token_ok = _token_path().exists()
    runners = MeetingSessionStore.live_runners() if enabled else []

    items = [
        {"ok": enabled, "label": _l("Fonctionnalité activée"),
         "remedy": _l("Utiliser le bouton « Activer » ci-dessus.")},
        {"ok": facade_on, "label": _l("Façade temps réel active"),
         "remedy": _l("Réactiver via le bouton « Activer » (elle est incluse).")},
        {"ok": key_present, "label": _l("Clé de chiffrement présente (.env)"),
         "remedy": _l("Relancer l'installation (elle la génère) puis redémarrer le service.")},
        {"ok": account is not None and token_ok, "label": _l("Compte et jeton d'exécutant provisionnés"),
         "remedy": _l("Utiliser le bouton « Activer » — le portail les crée lui-même.")},
        {"ok": bool(runners), "label": _l("Exécutant vivant (vu < 2 min)"),
         "remedy": _l("Démarrer le service : systemctl enable --now transcria-meeting-runner")},
    ]
    covered = set()
    for r in runners:
        try:
            covered.update(json.loads(r.platforms_json))
        except ValueError:
            pass
    items.append({"ok": "jitsi" in covered, "label": _l("Jitsi couvert par un exécutant"),
                  "remedy": _l("Vérifier `platforms: [jitsi]` dans runner.yaml puis redémarrer l'exécutant.")})
    # Présence RÉELLE des images de bot, annoncée par les exécutants (vécu : « code 125 »
    # cryptique quand l'image manquait — désormais la check-list le dit AVANT la réunion).
    image_ok, image_seen = False, False
    for r in runners:
        try:
            for img in json.loads(r.images_json or "[]"):
                if isinstance(img, dict) and img.get("provider") == "jitsi":
                    image_seen = True
                    image_ok = image_ok or bool(img.get("present"))
        except ValueError:
            pass
    items.append({"ok": image_ok if image_seen else not runners,
                  "label": _l("Image de bot Jitsi présente sur l'exécutant"),
                  "remedy": _l("Construire : docker build -f Dockerfile.bot -t transcria-bot:latest . "
                               "(ou renseigner une image GHCR dans runner.yaml).")})
    return items

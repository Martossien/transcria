"""Bilan de premier démarrage — l'accueil rend visible ce que le doctor sait déjà.

Constat (analyse installation 2026-08-06) : sur une install incomplète, l'accueil
invitait à « créer votre premier traitement » sans dire que des modèles manquaient
ou qu'aucun GPU n'était vu — le diagnostic existait (doctor CLI, page Modèles) mais
rien ne reliait ces pièces dans l'interface. Ce module compose les MÊMES sources que
les pages de réparation vers lesquelles la checklist pointe (catalogue de la page
Modèles, checks doctor légers) : la checklist et la page qui corrige ne peuvent pas
se contredire.

Uniquement des sondes bon marché (NVML, fichiers, résolution de binaire) — jamais de
smoke LLM ni de diff de schéma : le fragment est servi à chaque affichage de
l'accueil pour un admin. Le mot de passe par défaut n'est PAS repris ici : le bandeau
de session existant (``auth/routes.py``) couvre déjà ce cas sur toutes les pages.

Les items portent des FAITS structurés (``data``) ; la rédaction (et donc l'i18n
Flask-Babel) vit dans le template ``_first_run_checklist.html``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from transcria.auth.store import UserStore
from transcria.diagnostics.checks.common import OK, WARN
from transcria.diagnostics.checks.llm import check_opencode
from transcria.diagnostics.checks.remote import check_inference_nodes
from transcria.gpu import inventory
from transcria.llm_cloud.gate import cloud_status
from transcria.models_catalog import catalog_with_status, diarization_auto_recommendation


@dataclass
class FirstRunItem:
    """Un point du bilan. ``status`` reprend les statuts doctor (ok/warn/fail)."""

    key: str                                  # compute | models | opencode | diarization | notifications | llm_cloud
    status: str
    data: dict = field(default_factory=dict)  # faits structurés — le template rédige


def _compute_item(cfg: dict, cloud: dict | None = None) -> FirstRunItem:
    """GPU locaux, ou joignabilité des nœuds distants quand l'inférence est déportée."""
    mode = str((cfg.get("inference") or {}).get("mode", "local")).strip().lower()
    if mode != "local":
        res = check_inference_nodes(cfg)
        return FirstRunItem("compute", res.status, {"mode": mode, "detail": res.detail})
    states = inventory.snapshot()
    if states:
        total_gib = round(sum(s.total_gib for s in states), 1)
        return FirstRunItem("compute", OK, {"gpus": len(states), "vram_gib": total_gib})
    # Palier « zéro GPU » (docs/LLM_CLOUD.md § 4) : en cloud, les phases LLM sont servies par le
    # fournisseur et le STT/la diarisation tournent sur CPU — l'absence de GPU est un choix, pas
    # un point à finaliser (sinon la checklist d'accueil resterait affichée à vie).
    if (cloud or {}).get("active"):
        return FirstRunItem("compute", OK, {"gpus": 0, "llm_cloud": True})
    return FirstRunItem("compute", WARN, {"gpus": 0})


def _models_item(cfg: dict, total_vram_mb: int | None) -> FirstRunItem:
    """Modèles requis par CETTE config — même catalogue que la page Modèles."""
    view = catalog_with_status(cfg, total_vram_mb=total_vram_mb)
    # `served_instead` : la LLM du palier manque mais une autre est servie — pas un manque.
    # `optional` : lignes d'une recommandation (diarisation « auto ») — jamais un manque.
    missing = [it["spec"].label for it in view["items"]
               if not it["present"] and not it.get("served_instead") and not it.get("optional")]
    if not missing:
        return FirstRunItem("models", OK, {"total": len(view["items"])})
    return FirstRunItem("models", WARN, {"missing": missing})


def _opencode_item(cfg: dict) -> FirstRunItem:
    """Binaire opencode (phases LLM) — OK si les phases LLM sont désactivées."""
    res = check_opencode(cfg)
    return FirstRunItem("opencode", res.status, {"detail": res.detail})


def _diarization_item(cfg: dict) -> FirstRunItem:
    """Mise à niveau : proposer la diarisation « auto » aux configs antérieures à 0.4.7.

    OK dès que l'admin a activé « auto » (ou gardé son réglage via la page Modèles) —
    la recommandation vit dans le catalogue, même source que la page qui l'applique.
    """
    reco = diarization_auto_recommendation(cfg)
    if reco is None:
        return FirstRunItem("diarization", OK)
    return FirstRunItem("diarization", WARN, dict(reco))


def _llm_cloud_item(cloud: dict) -> FirstRunItem | None:
    """LLM d'arbitrage cloud : validée par un administrateur ? (absent hors cloud).

    ``cloud_status`` lit la base seulement — JAMAIS le réseau du fournisseur (ce fragment est
    servi à chaque accueil admin ; la sonde ``GET /models`` reste au doctor et au worker).
    """
    if not cloud.get("active"):
        return None
    if cloud.get("acknowledged"):
        return FirstRunItem("llm_cloud", OK, {"provider": cloud.get("provider_label") or "", "model": cloud.get("model") or ""})
    return FirstRunItem("llm_cloud", WARN, {"provider": cloud.get("provider_label") or "", "model": cloud.get("model") or ""})


def _notifications_item(cfg: dict) -> FirstRunItem:
    """Les utilisateurs seront-ils prévenus ? (retours utilisateurs 2026-11).

    Les e-mails « pré-analyse prête, à valider » et « transcription terminée » existent
    depuis longtemps, mais ``notifications.email.enabled`` est à ``false`` par défaut et
    l'adresse des comptes est vide par défaut : le ressenti « on n'est pas prévenu » vient
    de là, pas du code. Faits remontés : SMTP désactivé ; activé mais hôte/expéditeur
    manquant ; comptes actifs sans adresse.
    """
    email = (cfg.get("notifications") or {}).get("email") or {}
    if not bool(email.get("enabled")):
        return FirstRunItem("notifications", WARN, {"enabled": False})
    smtp_ok = bool(str(email.get("smtp_host") or "").strip()) and bool(str(email.get("from_address") or "").strip())
    try:
        without = UserStore.count_active_without_email()
    except Exception:  # noqa: BLE001 — hors contexte DB (outillage) : on ne bloque pas le bilan
        without = 0
    status = OK if smtp_ok and without == 0 else WARN
    return FirstRunItem("notifications", status, {"enabled": True, "smtp_ok": smtp_ok, "users_without_email": without})


def first_run_report(cfg: dict, *, total_vram_mb: int | None = None) -> list[FirstRunItem]:
    """Tous les points du bilan (statut OK compris) — :func:`needs_attention` filtre."""
    cloud = cloud_status(cfg)
    items = [
        _compute_item(cfg, cloud),
        _models_item(cfg, total_vram_mb),
        _opencode_item(cfg),
    ]
    cloud_item = _llm_cloud_item(cloud)
    if cloud_item is not None:
        items.append(cloud_item)
    items += [_diarization_item(cfg), _notifications_item(cfg)]
    return items


def needs_attention(items: list[FirstRunItem]) -> list[FirstRunItem]:
    """Les points à montrer — la checklist disparaît quand tout est vert."""
    return [it for it in items if it.status != OK]

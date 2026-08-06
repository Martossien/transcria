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

from transcria.diagnostics.checks.common import OK, WARN
from transcria.diagnostics.checks.llm import check_opencode
from transcria.diagnostics.checks.remote import check_inference_nodes
from transcria.gpu import inventory
from transcria.models_catalog import catalog_with_status


@dataclass
class FirstRunItem:
    """Un point du bilan. ``status`` reprend les statuts doctor (ok/warn/fail)."""

    key: str                                  # compute | models | opencode
    status: str
    data: dict = field(default_factory=dict)  # faits structurés — le template rédige


def _compute_item(cfg: dict) -> FirstRunItem:
    """GPU locaux, ou joignabilité des nœuds distants quand l'inférence est déportée."""
    mode = str((cfg.get("inference") or {}).get("mode", "local")).strip().lower()
    if mode != "local":
        res = check_inference_nodes(cfg)
        return FirstRunItem("compute", res.status, {"mode": mode, "detail": res.detail})
    states = inventory.snapshot()
    if states:
        total_gib = round(sum(s.total_gib for s in states), 1)
        return FirstRunItem("compute", OK, {"gpus": len(states), "vram_gib": total_gib})
    return FirstRunItem("compute", WARN, {"gpus": 0})


def _models_item(cfg: dict, total_vram_mb: int | None) -> FirstRunItem:
    """Modèles requis par CETTE config — même catalogue que la page Modèles."""
    view = catalog_with_status(cfg, total_vram_mb=total_vram_mb)
    missing = [it["spec"].label for it in view["items"] if not it["present"]]
    if not missing:
        return FirstRunItem("models", OK, {"total": len(view["items"])})
    return FirstRunItem("models", WARN, {"missing": missing})


def _opencode_item(cfg: dict) -> FirstRunItem:
    """Binaire opencode (phases LLM) — OK si les phases LLM sont désactivées."""
    res = check_opencode(cfg)
    return FirstRunItem("opencode", res.status, {"detail": res.detail})


def first_run_report(cfg: dict, *, total_vram_mb: int | None = None) -> list[FirstRunItem]:
    return [
        _compute_item(cfg),
        _models_item(cfg, total_vram_mb),
        _opencode_item(cfg),
    ]


def needs_attention(items: list[FirstRunItem]) -> list[FirstRunItem]:
    """Les points à montrer — la checklist disparaît quand tout est vert."""
    return [it for it in items if it.status != OK]

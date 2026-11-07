"""Vues partagées des moteurs STT SERVIS (page Modèles, formulaire Réglages) — activation en un clic.

Le projet construit lui-même audio.cpp / parakeet.cpp ; l'administrateur ne doit jamais avoir à
recopier un bloc YAML pour s'en servir. Ici : l'état de chaque moteur pour la page Modèles, et
l'auto-provisionnement (route + moteur déclaré, carte la plus libre) quand un moteur servi est
choisi sans route. Logique PURE dans ``config/served_stt_provisioning`` ; ici seulement la
composition et les messages.
"""
from __future__ import annotations

from dataclasses import dataclass

from flask_babel import gettext as _

from transcria.config.served_stt_provisioning import ServedProvision, apply_in_memory, pick_gpu, plan_served_backend
from transcria.gpu import inventory
from transcria.models_catalog import served_stt_weights_present
from transcria.stt.served_runtimes import (
    RUNTIME_INSTALL_COMMAND,
    SERVED_STT,
    referenced_served_backends,
    routed_url,
    runtime_present,
)

TARGETS = ("pipeline", "summary")


@dataclass(frozen=True)
class ProvisionOutcome:
    config: dict
    provision: ServedProvision | None
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    infos: tuple[str, ...] = ()


def runtime_missing_message(name: str) -> str:
    rt = SERVED_STT[name]
    return _("Moteur %(n)s : runtime %(r)s non installé sur cette machine. Lancez « %(cmd)s » puis "
             "réessayez (ou choisissez un autre moteur).",
             n=name, r=rt.runtime, cmd=RUNTIME_INSTALL_COMMAND[rt.runtime])


def provision_backend(cfg: dict, name: str) -> ProvisionOutcome:
    """Pose route + moteur pour ``name`` dans une copie de ``cfg`` (runtime requis, poids = avertissement)."""
    if not runtime_present(name):
        return ProvisionOutcome(cfg, None, errors=(runtime_missing_message(name),))
    prov = plan_served_backend(cfg, name, gpu_index=pick_gpu(inventory.snapshot()))
    warnings = list(prov.warnings)
    if not served_stt_weights_present(name):
        warnings.append(_("Les poids du moteur %(n)s ne sont pas encore téléchargés : page Modèles, "
                          "ligne « STT servi ».", n=name))
    info = _("Moteur %(n)s auto-configuré sur cette machine : port %(p)s, carte GPU %(g)s, lancé à la demande "
             "par TranscrIA. Modifiable dans Réglages → YAML (inference.stt.backends, resource_node.engines).",
             n=name, p=prov.port, g=prov.gpu)
    return ProvisionOutcome(apply_in_memory(cfg, prov), prov, warnings=tuple(warnings), infos=(info,))


def auto_provision(merged: dict) -> ProvisionOutcome:
    """Formulaire Réglages : chaque moteur servi choisi (pipeline, résumé) sans route est provisionné ;
    un runtime absent bloque la sauvegarde avec la commande à lancer."""
    errors: list[str] = []
    warnings: list[str] = []
    infos: list[str] = []
    cfg = merged
    for name in referenced_served_backends(merged):
        if routed_url(cfg, name):
            continue
        out = provision_backend(cfg, name)
        cfg = out.config
        errors.extend(out.errors)
        warnings.extend(out.warnings)
        infos.extend(out.infos)
    return ProvisionOutcome(cfg, None, errors=tuple(errors), warnings=tuple(warnings), infos=tuple(infos))


def served_stt_rows(cfg: dict) -> list[dict]:
    """Les moteurs servis connus, avec ce que la page Modèles doit dire de chacun (sans réseau)."""
    models = cfg.get("models") or {}
    rows = []
    for name, rt in SERVED_STT.items():
        rows.append({
            "name": name, "label": rt.label, "runtime": rt.runtime, "port": rt.port,
            "install_cmd": RUNTIME_INSTALL_COMMAND[rt.runtime],
            "runtime_present": runtime_present(name),
            "weights_present": served_stt_weights_present(name),
            "routed": bool(routed_url(cfg, name)),
            "is_pipeline": models.get("stt_backend") == name,
            "is_summary": models.get("summary_stt_backend") == name,
        })
    return rows

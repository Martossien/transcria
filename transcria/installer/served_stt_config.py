"""Fin de phase audiocpp / parakeetcpp : DÉCLARER le moteur servi dans config.yaml.

Jusqu'ici la phase construisait le runtime puis disait « déclarer le moteur dans
resource_node.engines + inference.stt.backends (cf. config.example.yaml) » — un bloc YAML à
recopier. Elle pose désormais elle-même la route loopback et le moteur (port conventionnel,
carte la plus libre), SANS choisir le moteur comme backend : l'admin le sélectionne ensuite
dans Réglages ou sur la page Modèles. Best-effort : une config absente (phase lancée avant
``config.yaml``) ou illisible n'échoue pas la phase, elle l'annonce.
"""
from __future__ import annotations

from pathlib import Path

from transcria.config.served_stt_provisioning import apply_to_file, pick_gpu, plan_served_backend
from transcria.config.yaml_file import load_yaml_file
from transcria.gpu import inventory
from transcria.installer.console import Console
from transcria.stt.served_runtimes import SERVED_STT, routed_url


def declare_served_engine(config_path: str | Path | None, backend: str, console: Console) -> bool:
    """Rend True si le moteur est déclaré (déjà ou à l'instant), False si rien n'a pu être écrit."""
    if backend not in SERVED_STT:
        return False
    if not config_path:
        console.info(f"Moteur {backend} : pas de config.yaml à compléter (relancer avec --config après "
                     "la génération de la configuration, ou activer depuis la page Modèles).")
        return False
    path = Path(config_path)
    if not path.is_file():
        console.info(f"Moteur {backend} : {path} absent — à activer plus tard depuis la page Modèles.")
        return False
    try:
        cfg = load_yaml_file(path)
        if routed_url(cfg, backend):
            console.ok(f"Moteur {backend} déjà déclaré ({routed_url(cfg, backend)}) — inchangé.")
            return True
        prov = plan_served_backend(cfg, backend, gpu_index=pick_gpu(inventory.snapshot()))
        apply_to_file(path, prov)
    except Exception as exc:  # noqa: BLE001 — best-effort : la phase a réussi, la déclaration se refait depuis l'UI
        console.warn(f"Moteur {backend} non déclaré dans {path} ({exc}) — activable depuis la page Modèles.")
        return False
    for warning in prov.warnings:
        console.warn(warning)
    console.ok(f"Moteur {backend} déclaré dans {path.name} : port {prov.port}, carte GPU {prov.gpu}, lancé à la "
               "demande — choisissez-le dans Réglages → Backend STT ou sur la page Modèles.")
    return True

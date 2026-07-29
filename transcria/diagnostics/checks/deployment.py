"""Doctor — déploiement : profil effectif, unités systemd, nœud de ressources (auth/moteurs/ports)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from transcria.diagnostics.checks.common import (
    _VALID_PROFILES,
    FAIL,
    OK,
    WARN,
    CheckResult,
    _redact_uri,
    _resolve_database_uri,
    _t,
)
from transcria.diagnostics.checks.probes import (
    _probe_openai_models,
    _systemd_unit_state,
    _tcp_port_open,
)


def check_deployment_profile(cfg: dict, *, profile: str | None = None) -> CheckResult:
    """Valide les invariants de haut niveau du profil d'installation demandé.

    Ce check ne remplace pas les vérifications spécialisées (DB, stockage, nœuds),
    il vérifie que le rôle runtime et le type de base ne contredisent pas le profil
    annoncé par l'installateur.
    """
    name = _t("chk_deploy_profile")
    if not profile:
        role = _effective_runtime_role(cfg)
        return CheckResult(name, OK, _t("dp_none", role=role))
    if profile not in _VALID_PROFILES:
        return CheckResult(
            name, FAIL, _t("dp_unknown", profile=profile),
            hint=_t("dp_unknown_hint", profiles=", ".join(_VALID_PROFILES)),
        )

    role = _effective_runtime_role(cfg)
    uri = _resolve_database_uri(cfg)
    is_postgres = str(uri).startswith("postgresql")
    expected_role = {
        "all-in-one": "all",
        "web": "web",
        "scheduler": "scheduler",
    }.get(profile)
    if expected_role and role != expected_role:
        return CheckResult(
            name, FAIL, _t("dp_role_mismatch", profile=profile, role=role, expected=expected_role),
            hint=_t("dp_role_mismatch_hint"),
        )
    if profile in ("web", "scheduler", "migrate") and not is_postgres:
        return CheckResult(
            name, FAIL, _t("dp_needs_pg", profile=profile, uri=_redact_uri(uri)),
            hint=_t("dp_needs_pg_hint"),
        )
    if profile == "resource-node":
        return CheckResult(name, OK, _t("dp_resource_node"))
    return CheckResult(name, OK, _t("dp_ok", profile=profile, role=role))

def check_systemd_profile(
    cfg: dict,
    *,
    profile: str | None = None,
    unit_state: Callable[[str], tuple[bool, bool] | None] | None = None,
) -> CheckResult:
    """Signale les conflits de services systemd pour un profil.

    Best-effort : en dev, Docker ou avec `--no-service`, systemd peut être absent.
    On retourne alors OK avec un détail explicite. Les conflits connus sont des WARN,
    pas des FAIL, car l'opérateur peut volontairement cohéberger certains rôles.
    """
    name = _t("chk_systemd_profile")
    if not profile:
        return CheckResult(name, OK, _t("sp_none"))
    probe = unit_state or _systemd_unit_state

    legacy = "transcria.service"
    split_units = ("transcria-web.service", "transcria-scheduler.service")
    resource_unit = "transcria-inference.service"
    conflicts_by_profile = {
        "all-in-one": split_units,
        "web": (legacy,),
        "scheduler": (legacy,),
        "resource-node": (legacy, "transcria-web.service", "transcria-scheduler.service"),
        "migrate": (),
    }
    conflicts: list[str] = []
    saw_systemd = False
    for unit in conflicts_by_profile.get(profile, ()):
        state = probe(unit)
        if state is None:
            continue
        saw_systemd = True
        active, enabled = state
        if active or enabled:
            detail = []
            if active:
                detail.append(_t("sp_active"))
            if enabled:
                detail.append(_t("sp_enabled"))
            conflicts.append(f"{unit} ({', '.join(detail)})")

    # Sonde une unité attendue pour distinguer systemd absent de "aucun conflit".
    expected_probe = {
        "all-in-one": legacy,
        "web": "transcria-web.service",
        "scheduler": "transcria-scheduler.service",
        "resource-node": resource_unit,
        "migrate": "transcria-migrate.service",
    }.get(profile)
    if expected_probe and probe(expected_probe) is not None:
        saw_systemd = True

    if conflicts:
        return CheckResult(
            name,
            WARN,
            _t("sp_conflicts", profile=profile, list="; ".join(conflicts)),
            hint=_t("sp_conflicts_hint"),
        )
    if not saw_systemd:
        return CheckResult(name, OK, _t("sp_no_systemd"))
    return CheckResult(name, OK, _t("sp_ok", profile=profile))

def check_resource_node_auth(cfg: dict) -> CheckResult:
    """Un nœud de ressources exposé doit avoir une clé API configurée.

    L'application autorise le mode ouvert pour le développement local, mais le profil
    `resource-node` correspond à un service réseau appelé par une frontale distante :
    le doctor doit rendre l'oubli visible.
    """
    name = _t("chk_rn_auth")
    auth = ((cfg.get("inference") or {}).get("auth") or {})
    env_name = str(auth.get("api_key_env") or "TRANSCRIA_INFERENCE_API_KEY")
    env_value = os.environ.get(env_name)
    direct = auth.get("api_key")
    if env_value or direct:
        source = _t("rna_src_env", env=env_name) if env_value else _t("rna_src_config")
        return CheckResult(name, OK, _t("rna_ok", source=source))
    return CheckResult(
        name,
        FAIL,
        _t("rna_missing", env=env_name),
        hint=_t("rna_missing_hint", env=env_name),
    )

def _resolve_manifest_path(raw_path: str) -> str:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return str(path)
    return str((Path.cwd() / path).resolve())

def _effective_runtime_role(cfg: dict) -> str:
    return (
        os.environ.get("TRANSCRIA_ROLE")
        or (cfg.get("runtime") or {}).get("role")
        or "all"
    ).strip().lower()

def check_resource_node_engines(
    cfg: dict,
    *,
    is_file: Callable[[str], bool] = os.path.isfile,
    is_executable: Callable[[str], bool] = lambda p: os.access(p, os.X_OK),
    reserved_ports: set[int] | None = None,
) -> CheckResult:
    """Valide le manifeste `resource_node.engines` sans lancer de moteur.

    Le nœud peut servir la diarisation / empreinte vocale sans moteur STT déclaré :
    l'absence de moteur est donc un WARN. En revanche, un moteur déclaré doit être
    cohérent, sinon `/engines/ensure` échouera en production.
    """
    name = _t("chk_rn_engines")
    engines = ((cfg.get("resource_node") or {}).get("engines") or [])
    if not engines:
        return CheckResult(
            name,
            WARN,
            _t("rne_no_engine"),
            hint=_t("rne_no_engine_hint"),
        )
    if not isinstance(engines, list):
        return CheckResult(name, FAIL, _t("rne_not_list"))

    errors: list[str] = []
    warnings: list[str] = []
    seen_names: set[str] = set()
    seen_ports: set[int] = set()
    if reserved_ports is None:
        try:
            reserved_ports = {int(os.environ.get("INFERENCE_PORT", "8002"))}
        except ValueError:
            reserved_ports = {8002}

    for index, raw in enumerate(engines, start=1):
        if not isinstance(raw, dict):
            errors.append(_t("rne_entry_invalid", index=index))
            continue
        label = str(raw.get("name") or f"#{index}")
        missing = [key for key in ("name", "script", "gpu", "port") if raw.get(key) in (None, "")]
        if missing:
            errors.append(_t("rne_missing_fields", label=label, fields=", ".join(missing)))
            continue

        engine_name = str(raw["name"]).strip()
        if engine_name in seen_names:
            errors.append(_t("rne_dup_name", name=engine_name))
        seen_names.add(engine_name)

        try:
            port = int(raw["port"])
            if port < 1 or port > 65535:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(_t("rne_bad_port", name=engine_name, port=repr(raw.get("port"))))
            continue
        if port in seen_ports:
            errors.append(_t("rne_dup_port", name=engine_name, port=port))
        seen_ports.add(port)
        if port in reserved_ports:
            errors.append(_t("rne_reserved_port", name=engine_name, port=port))

        try:
            gpu = int(raw["gpu"])
            if gpu < 0:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(_t("rne_bad_gpu", name=engine_name, gpu=repr(raw.get("gpu"))))

        try:
            gpu_mem = float(raw.get("gpu_mem", 0.85))
            if gpu_mem <= 0 or gpu_mem > 1:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(_t("rne_bad_gpu_mem", name=engine_name, gpu_mem=repr(raw.get("gpu_mem"))))

        script = _resolve_manifest_path(str(raw["script"]))
        if not is_file(script):
            errors.append(_t("rne_script_missing", name=engine_name, script=script))
        elif not is_executable(script):
            warnings.append(_t("rne_script_not_exec", name=engine_name, script=script))

    if errors:
        return CheckResult(
            name,
            FAIL,
            "; ".join(errors),
            hint=_t("rne_errors_hint"),
        )
    if warnings:
        return CheckResult(
            name,
            WARN,
            "; ".join(warnings),
            hint=_t("rne_warnings_hint"),
        )
    return CheckResult(name, OK, _t("rne_ok", n=len(engines)))

def check_resource_node_ports(
    cfg: dict,
    *,
    port_probe: Callable[[int], bool] = _tcp_port_open,
    models_probe: Callable[[int], dict | None] | None = None,
) -> CheckResult:
    """Vérifie que les ports STT déclarés sont libres ou déjà occupés par un STT sain."""
    name = _t("chk_rn_ports")
    engines = ((cfg.get("resource_node") or {}).get("engines") or [])
    if not isinstance(engines, list) or not engines:
        return CheckResult(name, OK, _t("rnp_none"))

    models_probe = models_probe or _probe_openai_models
    occupied_by_engine: list[str] = []
    free_ports: list[str] = []
    conflicts: list[str] = []

    for raw in engines:
        if not isinstance(raw, dict):
            continue
        engine_name = str(raw.get("name") or "?")
        port_raw = raw.get("port")
        if port_raw is None:
            continue
        try:
            port = int(port_raw)
            if port < 1 or port > 65535:
                continue
        except (TypeError, ValueError):
            continue

        if not port_probe(port):
            free_ports.append(f"{engine_name}:{port}")
            continue
        models = models_probe(port)
        if models and models.get("data"):
            active = str((models.get("data") or [{}])[0].get("id") or _t("rnp_unknown_model"))
            occupied_by_engine.append(f"{engine_name}:{port} ({active})")
        else:
            conflicts.append(f"{engine_name}:{port}")

    if conflicts:
        return CheckResult(
            name,
            FAIL,
            _t("rnp_conflicts", list=", ".join(conflicts)),
            hint=_t("rnp_conflicts_hint"),
        )
    details = []
    if free_ports:
        details.append(_t("rnp_free", list=", ".join(free_ports)))
    if occupied_by_engine:
        details.append(_t("rnp_active", list=", ".join(occupied_by_engine)))
    return CheckResult(name, OK, "; ".join(details) if details else _t("rnp_ok"))

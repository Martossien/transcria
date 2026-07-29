"""Préflight de diagnostic « transcria doctor » — FAÇADE + CLI + registre des profils.

Détecte *avant* d'exécuter un job les pannes classiques qui, sinon, se traduisent par des
jobs en échec sans cause lisible (config illisible, schéma de base dérivé, LLM injoignable,
opencode absent, nœud distant muet, dossiers non inscriptibles…).

Depuis la vague 0 de consolidation (2026-07), les vérifications vivent dans
``transcria/diagnostics/checks/`` par DOMAINE (database, llm, remote, storage, deployment,
identity — socle ``common``, sondes ``probes``). Ce module reste le POINT D'ENTRÉE unique :

- il RÉ-EXPORTE toute la surface publique historique (``check_*``, ``CheckResult``,
  ``diff_live_schema``…) — appelants et tests ne changent pas ; le golden
  ``tests/test_doctor_registry_golden.py`` verrouille cette surface et la composition
  des registres ;
- il porte les registres ``_CHECKS`` / ``_PROFILE_CHECKS`` (quels checks pour quel profil),
  l'orchestration (``run_doctor``), le rendu (``format_report``) et la CLI (``main``).

Chaque vérification est isolée (une exception devient un ``fail`` explicite) et ses
dépendances effectives sont injectables — voir ``checks/probes.py``. Lancement :
``venv/bin/python scripts/doctor.py`` ou ``python -m transcria.diagnostics.doctor``.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Callable

from transcria.diagnostics.checks.common import (  # noqa: F401 — façade
    _LABELS,
    _SYMBOLS,
    _VALID_PROFILES,
    EXIT_FAIL,
    EXIT_OK,
    FAIL,
    OK,
    WARN,
    CheckResult,
    _redact_uri,
    _resolve_database_uri,
    _t,
)
from transcria.diagnostics.checks.database import (  # noqa: F401 — façade
    _register_models,
    check_database,
    check_database_encoding,
    diff_live_schema,
)
from transcria.diagnostics.checks.deployment import (  # noqa: F401 — façade
    check_deployment_profile,
    check_resource_node_auth,
    check_resource_node_engines,
    check_resource_node_ports,
    check_systemd_profile,
)
from transcria.diagnostics.checks.identity import (  # noqa: F401 — façade
    check_identity_backend,
    check_transport_security,
)
from transcria.diagnostics.checks.llm import (  # noqa: F401 — façade
    check_arbitrage_llm,
    check_arbitrage_script,
    check_opencode,
    check_opencode_model_resolution,
    check_opencode_smoke,
)
from transcria.diagnostics.checks.probes import (  # noqa: F401 — façade
    _dir_writable,
    _probe_node_capabilities,
    _probe_node_health,
    _probe_openai_models,
    _probe_server_encoding,
    _systemd_unit_state,
    _tcp_port_open,
)
from transcria.diagnostics.checks.remote import (  # noqa: F401 — façade
    check_inference_node_gpus,
    check_inference_nodes,
    check_remote_stt_control_plane,
    check_served_stt_runtimes,
    check_stt_instances_vram,
)
from transcria.diagnostics.checks.storage import (  # noqa: F401 — façade
    check_disk_space,
    check_local_models,
    check_shared_storage,
    check_storage,
    expected_model_assets,
)


def _load_env_for_doctor(config_path: str | None) -> None:
    """Charge `.env` comme les services, sans écraser l'environnement courant."""
    try:
        from dotenv import load_dotenv
    except Exception:  # noqa: BLE001 — le doctor doit rester robuste même si dotenv manque
        return
    env_file = os.environ.get("ENV_FILE")
    if env_file:
        load_dotenv(env_file, override=False)
        return
    cfg_path = config_path or os.environ.get("TRANSCRIA_CONFIG") or "config.yaml"
    try:
        candidate = Path(cfg_path).resolve().parent / ".env"
    except Exception:  # noqa: BLE001
        candidate = Path(".env").resolve()
    load_dotenv(candidate, override=False)

_CHECKS: tuple[Callable[[dict], CheckResult], ...] = (
    check_database,
    check_database_encoding,
    check_arbitrage_script,
    check_arbitrage_llm,
    check_opencode,
    check_opencode_model_resolution,
    check_inference_nodes,
    check_remote_stt_control_plane,
    check_served_stt_runtimes,
    check_stt_instances_vram,
    check_identity_backend,
    check_transport_security,
    check_inference_node_gpus,
    check_local_models,
    check_storage,
    check_disk_space,
    check_shared_storage,
)

_PROFILE_CHECKS: dict[str, tuple[Callable[[dict], CheckResult], ...]] = {
    "all-in-one": _CHECKS,
    "web": (
        check_database,
        check_database_encoding,
        check_inference_nodes,
        check_remote_stt_control_plane,
        check_inference_node_gpus,
        check_storage,
        check_shared_storage,
    ),
    "scheduler": _CHECKS,
    "resource-node": (
        check_resource_node_auth,
        check_resource_node_engines,
        check_served_stt_runtimes,
        check_resource_node_ports,
        check_local_models,
    ),
    "migrate": (
        check_database,
        check_database_encoding,
    ),
}

def _checks_for_profile(profile: str | None) -> tuple[Callable[[dict], CheckResult], ...]:
    if not profile:
        return _CHECKS
    return _PROFILE_CHECKS.get(profile, ())

def run_doctor(
    config_path: str | None = None,
    *,
    loader: Callable[..., dict] | None = None,
    llm_smoke: bool = False,
    profile: str | None = None,
) -> list[CheckResult]:
    """Charge la config puis exécute toutes les vérifications. La config illisible
    court-circuite (un seul ``fail``, le reste dépend d'elle).

    `llm_smoke=True` ajoute le test réel opencode→LLM→texte (opt-in, non GPU-free)."""
    _load_env_for_doctor(config_path)
    if loader is None:
        # Différé §8.3(c) : repli du seam injectable (PyYAML) — erreur lisible si absent.
        from transcria.config.loader import load_config

        loader = load_config
    try:
        cfg = loader(config_path)
    except Exception as exc:  # noqa: BLE001
        return [CheckResult(_t("chk_config"), FAIL, _t("cfg_load_failed", exc=exc),
                            hint=_t("cfg_load_failed_hint"))]

    path_used = config_path or os.environ.get("TRANSCRIA_CONFIG") or "config.yaml"
    results = [CheckResult(_t("chk_config"), OK, _t("cfg_loaded", path=path_used))]
    if profile:
        results.append(check_deployment_profile(cfg, profile=profile))
        results.append(check_systemd_profile(cfg, profile=profile))
    checks = _checks_for_profile(profile)
    checks = (*checks, check_opencode_smoke) if llm_smoke else checks
    for check in checks:
        try:
            results.append(check(cfg))
        except Exception as exc:  # noqa: BLE001 — une vérif ne doit jamais crasher le doctor
            results.append(CheckResult(getattr(check, "__name__", "check"), FAIL, _t("check_errored", exc=exc)))
    return results

def compute_exit_code(results: list[CheckResult], *, strict: bool = False) -> int:
    statuses = {r.status for r in results}
    if FAIL in statuses:
        return EXIT_FAIL
    if strict and WARN in statuses:
        return EXIT_FAIL
    return EXIT_OK

def format_report(results: list[CheckResult], *, color: bool | None = None) -> str:
    if color is None:
        color = sys.stdout.isatty()
    ansi = {OK: "\033[32m", WARN: "\033[33m", FAIL: "\033[31m"} if color else {}
    reset = "\033[0m" if color else ""

    lines = ["", _t("report_title"), "=" * 44]
    for r in results:
        col = ansi.get(r.status, "")
        lines.append(f"{col}{_SYMBOLS[r.status]} [{_LABELS[r.status]:>4}]{reset} {r.name} — {r.detail}")
        if r.hint and r.status != OK:
            lines.append(f"          ↳ {r.hint}")

    n_fail = sum(1 for r in results if r.status == FAIL)
    n_warn = sum(1 for r in results if r.status == WARN)
    n_ok = sum(1 for r in results if r.status == OK)
    lines.append("-" * 44)
    lines.append(_t("report_summary", ok=n_ok, warn=n_warn, fail=n_fail))
    if n_fail:
        lines.append(_t("report_has_fail"))
    elif n_warn:
        lines.append(_t("report_has_warn"))
    else:
        lines.append(_t("report_all_green"))
    lines.append("")
    return "\n".join(lines)

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="transcria doctor",
        description=_t("cli_description"),
    )
    parser.add_argument("--config", default=None, help=_t("cli_config"))
    parser.add_argument("--profile", choices=_VALID_PROFILES, default=None,
                        help=_t("cli_profile"))
    parser.add_argument("--strict", action="store_true", help=_t("cli_strict"))
    parser.add_argument("--json", action="store_true", help=_t("cli_json"))
    parser.add_argument("--llm-smoke", action="store_true",
                        help=_t("cli_llm_smoke"))
    args = parser.parse_args(argv)

    results = run_doctor(config_path=args.config, llm_smoke=args.llm_smoke, profile=args.profile)
    if args.json:
        import json

        print(json.dumps([r.__dict__ for r in results], ensure_ascii=False, indent=2))
    else:
        print(format_report(results))
    return compute_exit_code(results, strict=args.strict)


if __name__ == "__main__":
    raise SystemExit(main())

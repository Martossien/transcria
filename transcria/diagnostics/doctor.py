"""Préflight de diagnostic « transcria doctor » — sans GPU, sans effet de bord.

Détecte *avant* d'exécuter un job les pannes classiques qui, sinon, se traduisent
par des jobs en échec sans cause lisible :

- **config illisible** (YAML cassé) ;
- **schéma de base dérivé** — la colonne/table attendue par les modèles manque dans
  la base réelle (typiquement une base créée hors Alembic, ou un ``alembic upgrade
  head`` oublié après un ``git pull``) ;
- **script de lancement LLM manquant ou non exécutable** ;
- **LLM d'arbitrage injoignable** (avec le bon log à consulter) ;
- **binaire opencode absent** alors qu'une phase LLM est activée ;
- **modèle LLM non résolu par opencode** — le `model_id` du pipeline (ex.
  ``local/arbitrage``) n'a aucune clé correspondante dans le provider opencode
  (panne « aucun texte produit » sinon vue seulement au 1ᵉʳ résumé) ;
- **nœud de ressources distant injoignable** en topologie remote/hybrid ;
- **dossiers de travail non inscriptibles**.

Chaque vérification est isolée (une exception devient un ``fail`` explicite, jamais
un crash) et ses dépendances effectives (sonde réseau, accès disque, diff de schéma)
sont injectables pour les tests. Lancement : ``venv/bin/python scripts/doctor.py``
ou ``python -m transcria.diagnostics.doctor``.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from transcria.cli_i18n import make_translator
from transcria.database import MODEL_MODULES, db
from transcria.diagnostics.doctor_messages import DOCTOR_MESSAGES
from transcria.gpu.hardware_advisor import _detect_gpu_totals_mb
from transcria.gpu.stt_instance_planner import (
    DEFAULT_INSTANCE_VRAM_MB,
    DEFAULT_SAFETY_MARGIN_MB,
    llm_reserved_by_gpu,
)
from transcria.installer.audiocpp_phase import (
    AUDIOCPP_PINNED_COMMIT,
    audiocpp_home,
    audiocpp_is_complete,
    resolve_runtimes_dir,
)
from transcria.installer.parakeetcpp_phase import (
    PARAKEETCPP_PINNED_COMMIT,
    parakeetcpp_home,
    parakeetcpp_is_complete,
)

# Traducteur FR/EN des sorties du doctor (locale résolue depuis TRANSCRIA_DEFAULT_LOCALE,
# exporté par install.sh ; défaut fr ⇒ sortie historique inchangée). `_t(clé, **vars)`.
_t = make_translator(DOCTOR_MESSAGES)

OK = "ok"
WARN = "warn"
FAIL = "fail"

_SYMBOLS = {OK: "✓", WARN: "⚠", FAIL: "✗"}
_LABELS = {OK: "OK", WARN: "WARN", FAIL: "FAIL"}

EXIT_OK = 0
EXIT_FAIL = 1

_VALID_PROFILES = ("all-in-one", "web", "scheduler", "resource-node", "migrate")

# SOURCE UNIQUE des modules de modèles (cf. transcria.database.MODEL_MODULES) : le diff de
# schéma à chaud doit peupler db.metadata avec TOUTES les tables, sinon une table réelle
# (ex. job_timing, meeting_type_templates) est vue « en trop » ou son absence non détectée.
_MODEL_MODULES = MODEL_MODULES


@dataclass
class CheckResult:
    """Résultat d'une vérification. ``status`` ∈ {ok, warn, fail}."""

    name: str
    status: str
    detail: str
    hint: str | None = None


# ── Diff de schéma à chaud ────────────────────────────────────────────────


def _register_models() -> None:
    """Importe les modules de modèles → peuple ``transcria.database.db.metadata``."""
    import importlib

    for module in _MODEL_MODULES:
        importlib.import_module(module)


def _humanize_diff(diff) -> list[tuple[str, str]]:
    """Traduit une opération renvoyée par ``alembic.autogenerate.compare_metadata``
    en ``(severité, message)`` lisibles.

    ``compare_metadata`` décrit ce qu'il faudrait faire pour que la base **rejoigne**
    les modèles : ``add_*`` ⇒ l'objet manque dans la base (base en retard, cas le plus
    grave — c'est l'incident « colonne manquante »), ``remove_*`` ⇒ objet en trop dans
    la base (généralement bénin), ``modify_*`` ⇒ divergence à surveiller.
    """
    # Une entrée peut être une liste (diffs groupés au niveau table) ou un tuple.
    if isinstance(diff, list):
        out: list[tuple[str, str]] = []
        for sub in diff:
            out.extend(_humanize_diff(sub))
        return out

    op = diff[0]
    if op == "add_table":
        return [("missing", _t("diff_add_table", name=diff[1].name))]
    if op == "remove_table":
        return [("extra", _t("diff_remove_table", name=diff[1].name))]
    if op == "add_column":
        return [("missing", _t("diff_add_column", table=diff[2], col=diff[3].name))]
    if op == "remove_column":
        return [("extra", _t("diff_remove_column", table=diff[2], col=diff[3].name))]
    if op.startswith("modify_"):
        # ('modify_nullable'|'modify_type'|…, schema, table, column, …)
        table, column = diff[2], diff[3]
        return [("modify", _t("diff_modify", op=op, table=table, col=column))]
    return [("other", _t("diff_other", op=op))]


def diff_live_schema(database_uri: str) -> list[tuple[str, str]]:
    """Compare le schéma de la base **réelle** aux modèles SQLAlchemy.

    Retourne une liste ``(severité, message)`` ; liste vide ⇒ base alignée.
    ``compare_type``/``compare_server_default`` désactivés pour éviter les faux
    positifs entre dialectes : on cible la **présence** des tables/colonnes, ce qui
    suffit à attraper l'incident d'origine. Lève en cas de base injoignable
    (l'appelant transforme cela en ``fail``).
    """
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import create_engine

    _register_models()
    engine = create_engine(database_uri)
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(
                conn, opts={"compare_type": False, "compare_server_default": False}
            )
            raw = compare_metadata(ctx, db.metadata)
    finally:
        engine.dispose()

    findings: list[tuple[str, str]] = []
    for entry in raw:
        findings.extend(_humanize_diff(entry))
    return findings


# ── Vérifications individuelles ───────────────────────────────────────────


def check_database(
    cfg: dict,
    *,
    database_uri: str | None = None,
    differ: Callable[[str], list[tuple[str, str]]] = diff_live_schema,
) -> CheckResult:
    name = _t("chk_database")
    uri = database_uri or _resolve_database_uri(cfg)
    redacted = _redact_uri(uri)
    try:
        findings = differ(uri)
    except Exception as exc:  # noqa: BLE001 — toute panne de connexion = fail explicite
        return CheckResult(
            name, FAIL, _t("db_unreachable", uri=redacted, exc=exc),
            hint=_t("db_unreachable_hint"),
        )

    if not findings:
        return CheckResult(name, OK, _t("db_aligned", uri=redacted))

    missing = [msg for sev, msg in findings if sev == "missing"]
    others = [msg for sev, msg in findings if sev != "missing"]
    detail = "; ".join(missing + others)
    if missing:
        return CheckResult(
            name, FAIL, _t("db_drifted", detail=detail),
            hint=_t("db_drifted_hint"),
        )
    return CheckResult(
        name, WARN, _t("db_minor", detail=detail),
        hint=_t("db_minor_hint"),
    )


def check_database_encoding(
    cfg: dict,
    *,
    database_uri: str | None = None,
    prober: Callable[[str], str] | None = None,
) -> CheckResult:
    """La base PostgreSQL doit être en UTF8.

    `SQL_ASCII` (hérité d'un initdb sans locale) stocke les octets sans validation :
    pas de protection contre un client mal encodé, fonctions texte serveur byte-wise,
    et les clients qui ne forcent pas `client_encoding` reçoivent des `bytes`
    (psycopg3). L'app force client_encoding=utf8 (défense), mais la base doit être
    créée/migrée en UTF8 — procédure : docs/INSTALL.md, section « Encodage »."""
    name = _t("chk_db_encoding")
    uri = database_uri or _resolve_database_uri(cfg)
    if not uri.startswith("postgresql"):
        return CheckResult(name, OK, _t("enc_sqlite"))
    probe = prober or _probe_server_encoding
    try:
        encoding = str(probe(uri)).upper()
    except Exception as exc:  # noqa: BLE001 — toute panne de connexion = fail explicite
        return CheckResult(
            name, FAIL, _t("db_unreachable", uri=_redact_uri(uri), exc=exc),
            hint=_t("db_unreachable_hint"),
        )
    if encoding == "UTF8":
        return CheckResult(name, OK, _t("enc_utf8"))
    return CheckResult(
        name, WARN,
        _t("enc_other", encoding=encoding),
        hint=_t("enc_other_hint"),
    )


def check_arbitrage_script(
    cfg: dict,
    *,
    is_file: Callable[[str], bool] = os.path.isfile,
    is_executable: Callable[[str], bool] = lambda p: os.access(p, os.X_OK),
) -> CheckResult:
    name = _t("chk_arb_script")
    services = cfg.get("services", {})
    script = os.environ.get("TRANSCRIA_ARBITRAGE_SCRIPT") or services.get("arbitrage_script", "")
    if not script:
        return CheckResult(name, WARN, _t("arbs_none"),
                           hint=_t("arbs_none_hint"))
    if not is_file(script):
        return CheckResult(
            name, FAIL, _t("arbs_missing", script=script),
            hint=_t("arbs_missing_hint"),
        )
    if not is_executable(script):
        return CheckResult(name, WARN, _t("arbs_not_exec", script=script),
                           hint=f"chmod +x {script}")
    return CheckResult(name, OK, _t("arbs_ok", script=script))


def check_arbitrage_llm(
    cfg: dict,
    *,
    probe: Callable[[int], dict | None] | None = None,
) -> CheckResult:
    name = _t("chk_arb_llm")
    services = cfg.get("services", {})
    port = int(services.get("arbitrage_llm_port", services.get("qwen_port", 8080)))
    expected_model = (services.get("arbitrage_api_model_id") or "").strip()
    log_path = services.get("arbitrage_log_path") or f"/tmp/arbitrage_llm_{port}.log"

    probe = probe or _probe_openai_models
    try:
        models = probe(port)
    except Exception as exc:  # noqa: BLE001
        models = None
        _ = exc

    if not models:
        # Non bloquant : la LLM est lancée à la demande par le workflow.
        return CheckResult(
            name, WARN, _t("arbl_down", port=port),
            hint=_t("arbl_down_hint", log=log_path),
        )
    active = ""
    data = models.get("data") or []
    if data:
        active = data[0].get("id", "")
    if expected_model and active and active != expected_model:
        return CheckResult(
            name, WARN, _t("arbl_mismatch", port=port, active=active, expected=expected_model),
            hint=_t("arbl_mismatch_hint"),
        )
    return CheckResult(name, OK, _t("arbl_ok", port=port) + (_t("arbl_ok_model", active=active) if active else ""))


def check_opencode(
    cfg: dict,
    *,
    finder: Callable[..., str | None] | None = None,
) -> CheckResult:
    name = _t("chk_opencode")
    workflow = cfg.get("workflow", {})
    summary_on = workflow.get("summary_llm", {}).get("enabled", False)
    arbitration_on = workflow.get("arbitration_llm", {}).get("enabled", False)
    if not (summary_on or arbitration_on):
        return CheckResult(name, OK, _t("oc_disabled"))

    config_bin = workflow.get("arbitration_llm", {}).get("opencode_bin")
    if finder is None:
        # Différé §8.3(c) : repli du seam injectable — chargé seulement si ce check tourne.
        from transcria.gpu.opencode_setup import find_opencode_binary

        finder = find_opencode_binary
    resolved = finder(config_bin=config_bin)
    if not resolved:
        return CheckResult(
            name, FAIL, _t("oc_missing"),
            hint=_t("oc_missing_hint"),
        )
    return CheckResult(name, OK, _t("oc_found", resolved=resolved))


def _opencode_config_path() -> str:
    """Chemin du opencode.json qu'opencode lirait pour CET utilisateur.

    Suit la résolution d'opencode : ``OPENCODE_CONFIG`` explicite, sinon
    ``$XDG_CONFIG_HOME/opencode/opencode.json``, sinon ``~/.config/opencode/...``.
    """
    env = os.environ.get("OPENCODE_CONFIG")
    if env:
        return env
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "opencode", "opencode.json")


def _read_opencode_config(path: str) -> dict | None:
    """Lit et parse le opencode.json ; None si absent/illisible/non-objet."""
    import json

    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def check_opencode_model_resolution(
    cfg: dict,
    *,
    config_path: str | None = None,
    reader: Callable[[str], dict | None] | None = None,
) -> CheckResult:
    """Vérifie (statiquement, sans LLM) que le `model_id` du pipeline se RÉSOUT côté opencode.

    Le pipeline lance ``opencode run --model <workflow.arbitration_llm.model_id>``
    (ex. ``local/arbitrage``). Si le provider opencode n'expose pas une clé de modèle
    portant ce nom, l'appel échoue par « aucun texte produit » — panne silencieuse,
    diagnostiquée seulement au 1ᵉʳ résumé en prod (incident du 16/06/2026 : config
    opencode keyée ``qwen3-35b-arbitrage`` alors que le pipeline demandait ``arbitrage``).
    Ce contrôle est GPU-free et ne démarre PAS la LLM (contrairement au smoke opt-in) :
    il attrape le décalage à l'install / au doctor par défaut.
    """
    name = _t("chk_model_resolution")
    workflow = cfg.get("workflow", {})
    summary_on = workflow.get("summary_llm", {}).get("enabled", False)
    arbitration_on = workflow.get("arbitration_llm", {}).get("enabled", False)
    if not (summary_on or arbitration_on):
        return CheckResult(name, OK, _t("mr_disabled"))

    model_id = ((workflow.get("arbitration_llm", {}) or {}).get("model_id") or "").strip()
    if not model_id:
        return CheckResult(
            name, WARN, _t("mr_no_id"),
            hint=_t("mr_no_id_hint"),
        )
    if "/" not in model_id:
        return CheckResult(
            name, WARN, _t("mr_no_provider", model_id=model_id),
            hint=_t("mr_no_provider_hint"),
        )
    provider, _, model_key = model_id.partition("/")

    path = config_path or _opencode_config_path()
    reader = reader or _read_opencode_config
    data = reader(path)
    if data is None:
        return CheckResult(
            name, FAIL, _t("mr_no_config", path=path),
            hint=_t("mr_no_config_hint"),
        )
    providers = data.get("provider") or {}
    prov = providers.get(provider)
    if not isinstance(prov, dict) or not isinstance(prov.get("models"), dict):
        return CheckResult(
            name, FAIL, _t("mr_no_prov_key", provider=provider, path=path),
            hint=_t("mr_no_prov_key_hint", provider=provider),
        )
    models = prov["models"]
    if model_key not in models:
        available = ", ".join(models) or "(aucun)"
        return CheckResult(
            name, FAIL,
            _t("mr_no_model", provider=provider, model_key=model_key, available=available, model_id=model_id),
            hint=_t("mr_no_model_hint"),
        )
    return CheckResult(name, OK, _t("mr_ok", model_id=model_id, provider=provider, model_key=model_key))


def check_opencode_smoke(
    cfg: dict,
    *,
    runner_factory: Callable[..., Any] | None = None,
    probe: Callable[[int], dict | None] | None = None,
) -> CheckResult:
    """Test RÉEL opencode → LLM → texte (opt-in `--llm-smoke`).

    Lance opencode avec une consigne triviale et vérifie qu'il **produit du texte**.
    Attrape la classe de panne « opencode exit 0 mais 0 texte » (incident e62295c1).
    Nécessite la LLM d'arbitrage up et consomme de la VRAM — d'où l'opt-in (ce test
    rompt le contrat GPU-free / sans effet de bord du préflight par défaut).

    Pré-sonde le serveur LLM (`/v1/models`) AVANT opencode : si la LLM n'écoute pas, on
    échoue **immédiatement** avec une consigne claire au lieu d'attendre le timeout
    opencode (jusqu'à 120 s).
    """
    name = _t("chk_smoke")
    workflow = cfg.get("workflow", {})
    if not (workflow.get("summary_llm", {}).get("enabled", False)
            or workflow.get("arbitration_llm", {}).get("enabled", False)):
        return CheckResult(name, OK, _t("smoke_disabled"))

    import tempfile
    from pathlib import Path

    services = cfg.get("services", {})
    port = int(services.get("arbitrage_llm_port", services.get("qwen_port", 8080)))
    log_path = services.get("arbitrage_log_path") or f"/tmp/arbitrage_llm_{port}.log"

    # Pré-vol rapide : éviter un timeout opencode de ~120 s si la LLM est down.
    probe = probe or _probe_openai_models
    try:
        models = probe(port)
    except Exception:  # noqa: BLE001
        models = None
    if not models:
        return CheckResult(
            name, FAIL, _t("smoke_down", port=port),
            hint=_t("smoke_down_hint", log=log_path),
        )

    if runner_factory is None:
        # Différé §8.3(c) : repli du seam injectable — chargé seulement si ce check tourne.
        from transcria.gpu.opencode_runner import OpenCodeRunner

        runner_factory = OpenCodeRunner

    try:
        timeout_s = int(workflow.get("arbitration_llm", {}).get("smoke_timeout_seconds", 120))
    except (TypeError, ValueError):
        timeout_s = 120

    with tempfile.TemporaryDirectory(prefix="transcria_doctor_smoke_") as tmp:
        work = Path(tmp)
        prompt_file = work / "smoke_prompt.txt"
        prompt_file.write_text(_t("smoke_prompt_sys"), encoding="utf-8")
        runner = runner_factory(str(work), config=cfg)
        result = runner.run(
            _t("smoke_prompt_task"),
            str(prompt_file),
            timeout=timeout_s,
        )
        if not result.get("success"):
            return CheckResult(
                name, FAIL, _t("smoke_failed", error=result.get("error", _t("smoke_unknown_error"))),
                hint=_t("smoke_failed_hint", port=port, log=log_path),
            )
        smoke = work / "smoke.md"
        produced = bool(result.get("output")) or (
            smoke.is_file() and bool(smoke.read_text(encoding="utf-8").strip())
        )
        if not produced:
            return CheckResult(
                name, FAIL,
                _t("smoke_notext"),
                hint=_t("smoke_notext_hint", log=log_path),
            )
    return CheckResult(name, OK, _t("smoke_ok", port=port))


def check_inference_nodes(
    cfg: dict,
    *,
    health: Callable[[str], bool] | None = None,
) -> CheckResult:
    name = _t("chk_inference_nodes")
    inference = cfg.get("inference", {})
    mode = inference.get("mode", "local")
    if mode == "local":
        return CheckResult(name, OK, _t("in_local"))

    nodes = inference.get("nodes") or []
    urls = [n.get("url", "") for n in nodes if n.get("url")] if nodes else []
    if not urls and inference.get("url"):
        urls = [inference["url"]]
    if not urls:
        return CheckResult(name, WARN, _t("in_no_node", mode=mode),
                           hint=_t("in_no_node_hint"))

    health = health or _probe_node_health
    reachable = [u for u in urls if _safe_health(health, u)]
    fallback = bool(inference.get("fallback_local", True))
    if reachable:
        return CheckResult(name, OK, _t("in_reachable", n=len(reachable), total=len(urls), list=", ".join(reachable)))
    if fallback:
        return CheckResult(name, WARN, _t("in_degraded", total=len(urls)),
                           hint=_t("in_degraded_hint"))
    return CheckResult(name, FAIL, _t("in_down", total=len(urls)),
                       hint=_t("in_down_hint"))


def check_remote_stt_control_plane(cfg: dict) -> CheckResult:
    """Vérifie qu'un STT distant a aussi un nœud de contrôle pour `/engines/ensure`."""
    name = _t("chk_stt_control")
    inference = cfg.get("inference", {}) or {}
    mode = inference.get("mode", "local")
    stt_cfg = (inference.get("stt") or {}) if isinstance(inference.get("stt"), dict) else {}
    backends = (stt_cfg.get("backends") or {}) if isinstance(stt_cfg.get("backends"), dict) else {}
    remote_backends = sorted(
        name
        for name, spec in backends.items()
        if isinstance(spec, dict) and str(spec.get("url") or "").strip()
    )
    if not remote_backends:
        return CheckResult(name, OK, _t("stt_none"))
    if mode not in ("remote", "hybrid"):
        return CheckResult(
            name,
            WARN,
            _t("stt_mode", backends=", ".join(remote_backends), mode=mode),
            hint=_t("stt_mode_hint"),
        )

    nodes = inference.get("nodes") or []
    urls = [n.get("url", "") for n in nodes if isinstance(n, dict) and n.get("url")] if isinstance(nodes, list) else []
    if not urls and inference.get("url"):
        urls = [inference["url"]]
    if not urls:
        # All-in-one : un backend routé loopback avec un moteur homonyme déclaré dans
        # `resource_node.engines` est assuré EN PROCESS par le gate (pas besoin de nœud
        # de contrôle) — état sain, pas un oubli de config.
        from urllib.parse import urlparse

        declared = {str(e.get("name")) for e in ((cfg.get("resource_node", {}) or {}).get("engines") or [])
                    if isinstance(e, dict)}
        local_served = [
            b for b in remote_backends
            if (urlparse(str(backends[b].get("url"))).hostname in ("127.0.0.1", "localhost", "::1"))
            and b in declared
        ]
        if set(local_served) == set(remote_backends):
            return CheckResult(name, OK, _t("stt_local_served", backends=", ".join(local_served)))
        return CheckResult(
            name,
            WARN,
            _t("stt_no_control", backends=", ".join(remote_backends)),
            hint=_t("stt_no_control_hint"),
        )
    return CheckResult(name, OK, _t("stt_ok", n=len(remote_backends), m=len(urls)))


def check_served_stt_runtimes(cfg: dict) -> CheckResult:
    """Runtimes STT servis déclarés (qwen3asr/nemotron) : binaire provisionné + commit épinglé.

    Le manifeste `resource_node.engines` peut déclarer un moteur dont le runtime n'a
    jamais été construit (ou l'a été sur un ancien SHA après une montée de version) —
    le lanceur échouerait au premier job. On vérifie ici, avec la commande de reprise."""
    name = _t("chk_served_runtimes")
    engines = ((cfg.get("resource_node", {}) or {}).get("engines") or [])
    declared = {str(e.get("name")) for e in engines if isinstance(e, dict)}

    runtimes_dir = resolve_runtimes_dir()
    known = {
        "qwen3asr": ("audiocpp", lambda: audiocpp_is_complete(audiocpp_home(runtimes_dir), AUDIOCPP_PINNED_COMMIT)),
        "nemotron": ("parakeetcpp", lambda: parakeetcpp_is_complete(parakeetcpp_home(runtimes_dir), PARAKEETCPP_PINNED_COMMIT)),
    }
    concerned = sorted(declared & set(known))
    if not concerned:
        return CheckResult(name, OK, _t("served_rt_none"))
    missing = [e for e in concerned if not known[e][1]()]
    if missing:
        cli_names = ", ".join(known[e][0] for e in missing)
        return CheckResult(
            name, WARN,
            _t("served_rt_missing", engines=", ".join(missing)),
            hint=_t("served_rt_hint", cli=cli_names),
        )
    return CheckResult(name, OK, _t("served_rt_ok", engines=", ".join(concerned)))


def _caps_reports_gpu(capabilities: dict) -> bool:
    """True si `/capabilities` énumère au moins un GPU avec un `free_mb` lisible."""
    for gpu in capabilities.get("gpus", []) or []:
        if not isinstance(gpu, dict):
            continue
        raw = gpu.get("free_mb")
        if raw is None:
            continue
        try:
            int(raw)
        except (TypeError, ValueError):
            continue
        return True
    return False


def check_inference_node_gpus(
    cfg: dict,
    *,
    capabilities_probe: Callable[[str], dict | None] | None = None,
) -> CheckResult:
    """Un nœud de ressources joignable doit énumérer ses GPU (`free_mb`) via `/capabilities`.

    Détecté À L'INSTALLATION : sinon, en prod, les jobs distants défèrent **en silence**
    au pré-vol (`remote_vram_admits` → None faute de données GPU) au lieu d'être
    dispatchés normalement. Mieux vaut le voir au `doctor` que via des jobs qui stagnent.
    """
    name = _t("chk_node_gpus")
    inference = cfg.get("inference", {})
    mode = inference.get("mode", "local")
    if mode == "local":
        return CheckResult(name, OK, _t("ng_local"))

    nodes = inference.get("nodes") or []
    urls = [n.get("url", "") for n in nodes if n.get("url")] if nodes else []
    if not urls and inference.get("url"):
        urls = [inference["url"]]
    if not urls:
        return CheckResult(name, OK, _t("ng_no_node"))

    probe = capabilities_probe or _probe_node_capabilities
    reachable = [(u, caps) for u in urls if (caps := _safe_capabilities(probe, u)) is not None]
    if not reachable:
        return CheckResult(name, OK, _t("ng_no_caps"))

    without_gpu = [u for u, caps in reachable if not _caps_reports_gpu(caps)]
    if without_gpu:
        return CheckResult(
            name, WARN,
            _t("ng_without_gpu", n=len(without_gpu), total=len(reachable), list=", ".join(without_gpu)),
            hint=_t("ng_without_gpu_hint"),
        )
    return CheckResult(name, OK, _t("ng_ok", n=len(reachable)))


def check_storage(
    cfg: dict,
    *,
    is_writable: Callable[[str], bool] | None = None,
) -> CheckResult:
    name = _t("chk_storage")
    is_writable = is_writable or _dir_writable
    targets: list[tuple[str, str]] = []
    jobs_dir = cfg.get("storage", {}).get("jobs_dir", "./jobs")
    targets.append(("storage.jobs_dir", jobs_dir))
    voice = cfg.get("voice_enrollment", {})
    if voice.get("enabled"):
        targets.append(("voice_enrollment.storage_dir", voice.get("storage_dir", "./voices")))

    failures = [f"{label} ({path})" for label, path in targets if not is_writable(path)]
    if failures:
        return CheckResult(name, FAIL, _t("st_not_writable", list="; ".join(failures)),
                           hint=_t("st_not_writable_hint"))
    return CheckResult(name, OK, _t("st_ok", list=", ".join(f"{label}={path}" for label, path in targets)))


def check_disk_space(
    cfg: dict,
    *,
    usage_fn: Callable[[str], tuple[int, int]] | None = None,
) -> CheckResult:
    """Espace disque du dossier des jobs — un disque plein fait échouer les traitements
    de façon cryptique (C1.3). Seuils : < 2 Go = fail, < 10 Go = warn."""
    name = _t("chk_disk")
    jobs_dir = cfg.get("storage", {}).get("jobs_dir", "./jobs")

    def _usage(path: str) -> tuple[int, int]:
        import shutil as _sh

        probe = path
        while probe and not os.path.exists(probe):
            probe = os.path.dirname(probe.rstrip("/")) or "/"
        total, _used, free = _sh.disk_usage(probe or "/")
        return free, total

    usage_fn = usage_fn or _usage
    try:
        free_bytes, _total = usage_fn(jobs_dir)
    except OSError as exc:
        return CheckResult(name, WARN, _t("disk_unreadable", exc=exc))
    free_gb = free_bytes / (1024 ** 3)
    if free_gb < 2:
        return CheckResult(name, FAIL, _t("disk_fail", gb=f"{free_gb:.1f}", dir=jobs_dir),
                          hint=_t("disk_fail_hint"))
    if free_gb < 10:
        return CheckResult(name, WARN, _t("disk_warn", gb=f"{free_gb:.1f}", dir=jobs_dir),
                          hint=_t("disk_warn_hint"))
    return CheckResult(name, OK, _t("disk_ok", gb=f"{free_gb:.0f}", dir=jobs_dir))


def expected_model_assets(cfg: dict) -> list[tuple[str, str, str]]:
    """Modèles que CETTE machine doit avoir en cache local, d'après la config.

    Retourne des triplets ``(libellé, type, référence)`` avec type ∈ ``hf`` (id
    Hugging Face), ``path`` (chemin local), ``torchaudio`` (asset torch hub).
    Fonction pure et testable. Une phase servie À DISTANCE (``inference.mode:
    remote``) n'a pas besoin de ses poids ici ; en ``hybrid`` le repli local reste
    possible, donc on vérifie. Les modèles téléchargés au runtime échouent (ou
    pendent) derrière un proxy d'entreprise non configuré — cf. docs/INSTALL.md
    § « Réseau d'entreprise »."""
    assets: list[tuple[str, str, str]] = []
    remote_only = str((cfg.get("inference") or {}).get("mode", "local")).strip().lower() == "remote"

    def _kind(ref: str) -> str:
        return "path" if ref.startswith(("/", "./", "~")) else "hf"

    models = cfg.get("models", {}) or {}
    if not remote_only:
        stt = str(models.get("stt_backend", "cohere")).strip().lower()
        if stt == "cohere":
            ref = str((cfg.get("cohere") or {}).get("model_path", "CohereLabs/cohere-transcribe-03-2026"))
            assets.append(("STT Cohere", _kind(ref), ref))
        elif stt == "whisper":
            size = str((cfg.get("whisper") or {}).get("model_size", "large-v3"))
            assets.append(("STT Whisper", "hf", f"Systran/faster-whisper-{size}"))
        elif stt == "granite":
            ref = str((cfg.get("granite") or {}).get("model_id", "./models/granite-speech-4.1-2b"))
            assets.append(("STT Granite", _kind(ref), ref))
        elif stt == "parakeet":
            ref = str((cfg.get("parakeet") or {}).get("model_id", "nvidia/parakeet-tdt-0.6b-v3"))
            assets.append(("STT Parakeet", _kind(ref), ref))

        diar = str(models.get("diarization_backend", "pyannote")).strip().lower()
        if diar == "sortformer":
            ref = str((cfg.get("sortformer") or {}).get("model_id", "nvidia/diar_streaming_sortformer_4spk-v2.1"))
            assets.append(("Diarisation Sortformer", _kind(ref), ref))
        else:
            ref = str(models.get("model_id", "pyannote/speaker-diarization-community-1"))
            assets.append(("Diarisation pyannote", _kind(ref), ref))

    voice = cfg.get("voice_enrollment", {}) or {}
    if voice.get("enabled"):
        ref = str((voice.get("embedding") or {}).get("model_id", "pyannote/speaker-diarization-community-1"))
        if not any(r == ref for _, _, r in assets):
            assets.append(("Empreintes vocales", _kind(ref), ref))

    preflight = ((cfg.get("workflow") or {}).get("audio_preflight") or {})
    if preflight.get("enabled", True) and (preflight.get("squim") or {}).get("enabled"):
        assets.append(("SQUIM (préflight)", "torchaudio", "models/squim_objective_dns2020.pth"))
    return assets


def _model_asset_exists(kind: str, ref: str) -> bool:
    """Présence d'un modèle en cache local, sans réseau ni chargement."""
    if kind == "path":
        return Path(ref).expanduser().exists()
    if kind == "torchaudio":
        torch_home = Path(os.environ.get("TORCH_HOME", "~/.cache/torch")).expanduser()
        return (torch_home / "hub" / "torchaudio" / ref).is_file()
    hub = os.environ.get("HF_HUB_CACHE")
    hub_dir = Path(hub).expanduser() if hub else Path(
        os.environ.get("HF_HOME", "~/.cache/huggingface")
    ).expanduser() / "hub"
    model_dir = hub_dir / ("models--" + ref.replace("/", "--"))
    return model_dir.is_dir() and any(model_dir.rglob("*"))


def check_local_models(
    cfg: dict,
    *,
    asset_exists: Callable[[str, str], bool] | None = None,
) -> CheckResult:
    """Les modèles requis par la config doivent être en cache local.

    Un modèle absent est téléchargé au runtime : derrière un proxy d'entreprise non
    configuré dans l'environnement du service, ce téléchargement échoue — ou pend
    indéfiniment (incident SQUIM du 12/06/2026 : préflight gelé, job bloqué). Ce
    check rend le manque visible AVANT le premier job."""
    name = _t("chk_local_models")
    exists = asset_exists or _model_asset_exists
    assets = expected_model_assets(cfg)
    if not assets:
        return CheckResult(name, OK, _t("lm_none"))
    missing = [(label, ref) for label, kind, ref in assets if not exists(kind, ref)]
    if not missing:
        return CheckResult(name, OK, _t("lm_ok", n=len(assets)))
    return CheckResult(
        name, WARN,
        _t("lm_missing", list="; ".join(f"{label} ({ref})" for label, ref in missing)),
        hint=_t("lm_missing_hint"),
    )


def check_shared_storage(
    cfg: dict,
    *,
    table_exists: Callable[[str], bool] | None = None,
) -> CheckResult:
    """Topologie split : les fichiers de jobs doivent être visibles des deux tiers.

    En `role=web`/`scheduler` avec `shared_backend: fs`, rien ne garantit que la frontale
    et le worker voient le même `jobs_dir` (deux machines = audio introuvable côté worker,
    téléchargements 404 côté frontale). En backend `pg`, sonde l'existence des tables
    `job_files` (utile AVANT le premier démarrage de l'app, qui les crée sinon).
    Voir docs/STOCKAGE_PARTAGE_JOBS.md."""
    name = _t("chk_shared_storage")
    role = (
        os.environ.get("TRANSCRIA_ROLE")
        or (cfg.get("runtime") or {}).get("role")
        or "all"
    ).strip().lower()
    backend = str((cfg.get("storage") or {}).get("shared_backend") or "fs").strip().lower()

    if backend == "pg":
        url = (
            os.environ.get("TRANSCRIA_DATABASE_URL")
            or cfg.get("storage", {}).get("database_url", "")
        )
        if not str(url).startswith("postgresql"):
            return CheckResult(
                name, FAIL, _t("ss_pg_not_pg"),
                hint=_t("ss_pg_not_pg_hint"),
            )
        probe = table_exists or _job_files_table_exists
        try:
            ready = probe(str(url))
        except Exception as exc:  # noqa: BLE001 — panne de connexion = fail explicite
            return CheckResult(
                name, FAIL, _t("ss_pg_unreachable", exc=exc),
                hint=_t("ss_pg_unreachable_hint"),
            )
        if not ready:
            return CheckResult(
                name, FAIL, _t("ss_pg_no_tables"),
                hint=_t("ss_pg_no_tables_hint"),
            )
        return CheckResult(
            name, OK,
            _t("ss_pg_ok"),
        )
    if role in ("web", "scheduler"):
        return CheckResult(
            name, WARN,
            _t("ss_fs_split", role=role),
            hint=_t("ss_fs_split_hint"),
        )
    return CheckResult(name, OK, _t("ss_allinone"))


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


def _tcp_port_open(port: int, *, host: str = "127.0.0.1", timeout: float = 0.2) -> bool:
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


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


# ── Sondes / helpers effectifs (injectables ci-dessus) ────────────────────


def _probe_server_encoding(uri: str) -> str:
    """Sonde l'encodage serveur de la base PostgreSQL, hors process Flask."""
    from sqlalchemy import create_engine

    engine = create_engine(uri)
    try:
        with engine.connect() as conn:
            return str(conn.exec_driver_sql("SHOW server_encoding").scalar())
    finally:
        engine.dispose()


def _systemd_unit_state(unit: str) -> tuple[bool, bool] | None:
    """Retourne (active, enabled) ou None si systemd n'est pas utilisable ici."""
    if not shutil.which("systemctl"):
        return None

    def _run(*args: str) -> int:
        try:
            return subprocess.run(
                ["systemctl", *args, unit],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            ).returncode
        except (OSError, subprocess.SubprocessError):
            return 4

    active_rc = _run("is-active", "--quiet")
    enabled_rc = _run("is-enabled", "--quiet")
    if active_rc == 4 and enabled_rc == 4:
        return None
    return active_rc == 0, enabled_rc == 0


def _job_files_table_exists(uri: str) -> bool:
    """Sonde l'existence de la table `job_files` (backend pg), hors process Flask."""
    from sqlalchemy import create_engine, inspect

    engine = create_engine(uri)
    try:
        return bool(inspect(engine).has_table("job_files"))
    finally:
        engine.dispose()


def _probe_openai_models(port: int, timeout: int = 3) -> dict | None:
    """GET /v1/models sur le port local ; retourne le JSON ou None si injoignable."""
    import requests

    try:
        resp = requests.get(f"http://127.0.0.1:{port}/v1/models", timeout=timeout)
        if resp.status_code == 200:
            return resp.json()
    except Exception:  # noqa: BLE001
        return None
    return None


def _probe_node_health(url: str, timeout: int = 3) -> bool:
    import requests

    try:
        return requests.get(f"{url.rstrip('/')}/health", timeout=timeout).status_code == 200
    except Exception:  # noqa: BLE001
        return False


def _safe_health(health: Callable[[str], bool], url: str) -> bool:
    try:
        return bool(health(url))
    except Exception:  # noqa: BLE001
        return False


def _probe_node_capabilities(url: str, timeout: int = 3) -> dict | None:
    import requests

    try:
        resp = requests.get(f"{url.rstrip('/')}/capabilities", timeout=timeout)
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _safe_capabilities(probe: Callable[[str], dict | None], url: str) -> dict | None:
    try:
        result = probe(url)
        return result if isinstance(result, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _dir_writable(path: str) -> bool:
    """True si on peut écrire dans ``path`` (ou, s'il n'existe pas, dans son parent)."""
    if os.path.isdir(path):
        return os.access(path, os.W_OK)
    parent = os.path.dirname(os.path.abspath(path))
    return os.path.isdir(parent) and os.access(parent, os.W_OK)


def _resolve_database_uri(cfg: dict) -> str:
    return (
        os.environ.get("TRANSCRIA_DATABASE_URL")
        or cfg.get("storage", {}).get("database_url")
        or "sqlite:///transcrIA.db"
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


def _redact_uri(uri: str) -> str:
    try:
        from sqlalchemy.engine.url import make_url

        return make_url(uri).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001
        return uri


# ── Orchestration / rendu ─────────────────────────────────────────────────

def check_stt_instances_vram(
    cfg: dict,
    *,
    gpu_totals_provider: Callable[[], dict[int, int]] | None = None,
) -> CheckResult:
    """Cohérence VRAM des instances STT servies déclarées (lot conseiller matériel).

    Somme, par carte, les instances audiocpp déclarées (~6,5 Go pièce) + la
    réservation LLM déclarée + la marge : un dépassement du total de la carte
    = WARN (le pré-vol refusera ou thrashera en production). Sans GPU détectable
    ou sans instance servie : OK silencieux (rien à vérifier ici)."""
    name = _t("chk_stt_instances_vram")
    provider = gpu_totals_provider or _detect_gpu_totals_mb
    totals = provider()
    engines = ((cfg.get("resource_node") or {}).get("engines") or [])
    served = [e for e in engines if isinstance(e, dict) and any(
        marker in str(e.get("script") or "")
        for marker in ("qwen3asr", "nemotron", "parakeet", "audiocpp"))]
    if not totals or not served:
        return CheckResult(name, OK, _t("stt_inst_nothing"))

    per_gpu: dict[int, int] = {}
    for e in served:
        per_gpu[int(e.get("gpu", 0))] = per_gpu.get(int(e.get("gpu", 0)), 0) + 1
    reserved = llm_reserved_by_gpu(cfg)
    overflows: list[str] = []
    for gpu_index, count in sorted(per_gpu.items()):
        total = totals.get(gpu_index)
        if total is None:
            overflows.append(_t("stt_inst_unknown_gpu", gpu=gpu_index))
            continue
        need = count * DEFAULT_INSTANCE_VRAM_MB + reserved.get(gpu_index, 0) + DEFAULT_SAFETY_MARGIN_MB
        if need > total:
            overflows.append(_t("stt_inst_overflow", gpu=gpu_index, need=need, total=total))
    if overflows:
        return CheckResult(name, WARN, " ; ".join(overflows), hint=_t("stt_inst_hint"))
    return CheckResult(name, OK, _t("stt_inst_ok", n=len(served)))


def check_identity_backend(
    cfg: dict,
    *,
    discovery_prober: "Callable[[str], bool] | None" = None,
    admin_counter: "Callable[[], int] | None" = None,
    ldap_prober: "Callable[[str, int], bool] | None" = None,
) -> CheckResult:
    """Chantier identité : backend fédéré actif → IdP joignable ET break-glass garanti.

    - découverte OIDC (`{issuer}/.well-known/openid-configuration`) sondée en HTTP ;
    - LDAP : chaque contrôleur sondé au niveau TCP (host:port ouvert) + rappel LDAPS ;
    - au moins UN admin LOCAL actif doit exister (sinon une panne d'IdP verrouille
      tout le monde dehors — FAIL, cf. GESTION_IDENTITE §3.9)."""
    name = _t("chk_identity")
    backend = str(((cfg.get("auth", {}) or {}).get("backend")) or "local").strip().lower()
    if backend == "local":
        return CheckResult(name, OK, _t("idn_local"))

    problems: list[str] = []
    if admin_counter is None:
        def admin_counter() -> int:
            from transcria.auth.models import Role, User

            return User.query.filter_by(role=Role.ADMIN.value, is_active=True,
                                        identity_source="local").count()
    try:
        local_admins = admin_counter()
    except Exception as exc:  # noqa: BLE001 — base indisponible = diagnostic impossible
        return CheckResult(name, WARN, _t("idn_db_unavailable", exc=exc))
    if local_admins == 0:
        return CheckResult(name, FAIL, _t("idn_no_local_admin", backend=backend),
                           hint=_t("idn_no_local_admin_hint"))

    if backend == "oidc":
        issuer = str(((cfg.get("auth", {}) or {}).get("oidc", {}) or {}).get("issuer") or "").rstrip("/")
        if discovery_prober is None:
            def discovery_prober(url: str) -> bool:
                import requests

                try:
                    return requests.get(url, timeout=5).status_code == 200
                except Exception:  # noqa: BLE001
                    return False
        url = f"{issuer}/.well-known/openid-configuration"
        if not issuer or not discovery_prober(url):
            problems.append(_t("idn_discovery_ko", url=url))
    if backend == "proxy":
        # GESTION_IDENTITE §3.7 : un réseau de confiance trop large = n'importe
        # quelle machine peut poser Remote-User et devenir n'importe qui.
        import ipaddress

        for entry in ((cfg.get("auth", {}) or {}).get("proxy", {}) or {}).get("trusted_ips") or []:
            try:
                net = ipaddress.ip_network(str(entry).strip(), strict=False)
            except ValueError:
                continue  # le schéma de config refuse déjà l'entrée invalide
            if net.num_addresses > 65536:
                problems.append(_t("idn_proxy_open_network", entry=entry))
    if backend == "ldap":
        problems.extend(_check_ldap_reachability(cfg, ldap_prober))
    if problems:
        return CheckResult(name, WARN, " ; ".join(problems), hint=_t("idn_discovery_hint"))
    return CheckResult(name, OK, _t("idn_ok", backend=backend, admins=local_admins))


def _check_ldap_reachability(cfg: dict, ldap_prober) -> list[str]:
    """Sonde TCP de chaque contrôleur LDAP + rappels de sécurité (LDAPS, plaintext).

    On ne tente PAS de bind ici (pas de secret dans le doctor) : on vérifie que
    l'hôte:port répond, et on signale un canal non chiffré autorisé — c'est le
    diagnostic le plus utile sans divulguer d'identifiants."""
    import socket as _socket
    from urllib.parse import urlparse

    ldap_cfg = ((cfg.get("auth", {}) or {}).get("ldap", {}) or {})
    problems: list[str] = []
    servers = ldap_cfg.get("servers") or []
    if isinstance(servers, str):
        servers = [servers]
    use_ssl = bool(ldap_cfg.get("use_ssl", True))
    if not use_ssl and not bool(ldap_cfg.get("start_tls", False)) and bool(ldap_cfg.get("allow_plaintext", False)):
        problems.append(_t("idn_ldap_plaintext"))

    if ldap_prober is None:
        def ldap_prober(host: str, port: int) -> bool:
            try:
                with _socket.create_connection((host, port), timeout=5):
                    return True
            except OSError:
                return False

    for uri in servers:
        parsed = urlparse(str(uri) if "://" in str(uri) else f"ldap://{uri}")
        host = parsed.hostname or str(uri)
        port = parsed.port or (636 if (parsed.scheme == "ldaps" or use_ssl) else 389)
        if not ldap_prober(host, port):
            problems.append(_t("idn_ldap_unreachable", host=host, port=port))
    return problems


def check_transport_security(cfg: dict) -> CheckResult:
    """Posture HTTP(S) : un backend d'auth FÉDÉRÉ (mots de passe d'annuaire, jetons
    OIDC) sans cookie sécurisé ni proxy TLS déclaré = identifiants d'entreprise sur un
    transport potentiellement en clair. WARN ciblé (le HTTP reste légitime en dev/local)."""
    name = _t("chk_transport")
    sec = (cfg.get("security", {}) or {})
    backend = str(((cfg.get("auth", {}) or {}).get("backend")) or "local").strip().lower()
    secure = bool(sec.get("session_cookie_secure", False)) or bool(sec.get("behind_tls_proxy", False))
    if backend in ("oidc", "proxy", "ldap") and not secure:
        return CheckResult(name, WARN, _t("transport_federated_insecure", backend=backend),
                           hint=_t("transport_hint"))
    return CheckResult(name, OK, _t("transport_ok", secure="oui" if secure else "non (local/HTTP)"))


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


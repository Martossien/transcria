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
import sys
from dataclasses import dataclass
from typing import Callable

OK = "ok"
WARN = "warn"
FAIL = "fail"

_SYMBOLS = {OK: "✓", WARN: "⚠", FAIL: "✗"}
_LABELS = {OK: "OK", WARN: "WARN", FAIL: "FAIL"}

EXIT_OK = 0
EXIT_FAIL = 1

# Modules de modèles à importer pour peupler ``db.metadata`` (même liste que le
# test anti-dérive Alembic). Repris ici pour le diff de schéma à chaud.
_MODEL_MODULES = (
    "transcria.audit.models",
    "transcria.auth.models",
    "transcria.context.central_lexicon_models",
    "transcria.jobs.models",
    "transcria.queue.models",
    "transcria.voice.models",
)


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
        return [("missing", f"table absente de la base : {diff[1].name}")]
    if op == "remove_table":
        return [("extra", f"table en trop dans la base : {diff[1].name}")]
    if op == "add_column":
        return [("missing", f"colonne absente de la base : {diff[2]}.{diff[3].name}")]
    if op == "remove_column":
        return [("extra", f"colonne en trop dans la base : {diff[2]}.{diff[3].name}")]
    if op.startswith("modify_"):
        # ('modify_nullable'|'modify_type'|…, schema, table, column, …)
        table, column = diff[2], diff[3]
        return [("modify", f"divergence {op} sur {table}.{column}")]
    return [("other", f"divergence schéma : {op}")]


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

    from transcria.database import db

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
    name = "Base de données (schéma)"
    uri = database_uri or _resolve_database_uri(cfg)
    redacted = _redact_uri(uri)
    try:
        findings = differ(uri)
    except Exception as exc:  # noqa: BLE001 — toute panne de connexion = fail explicite
        return CheckResult(
            name, FAIL, f"base injoignable ({redacted}) : {exc}",
            hint="Vérifier que la base tourne et que TRANSCRIA_DATABASE_URL / storage.database_url est correct.",
        )

    if not findings:
        return CheckResult(name, OK, f"schéma aligné sur les modèles ({redacted})")

    missing = [msg for sev, msg in findings if sev == "missing"]
    others = [msg for sev, msg in findings if sev != "missing"]
    detail = "; ".join(missing + others)
    if missing:
        return CheckResult(
            name, FAIL, f"schéma dérivé — {detail}",
            hint="Base en retard sur les modèles : appliquer `alembic upgrade head`.",
        )
    return CheckResult(
        name, WARN, f"divergences mineures — {detail}",
        hint="Objets en trop / divergences : vérifier l'historique Alembic.",
    )


def check_arbitrage_script(
    cfg: dict,
    *,
    is_file: Callable[[str], bool] = os.path.isfile,
    is_executable: Callable[[str], bool] = lambda p: os.access(p, os.X_OK),
) -> CheckResult:
    name = "Script de lancement LLM d'arbitrage"
    services = cfg.get("services", {})
    script = os.environ.get("TRANSCRIA_ARBITRAGE_SCRIPT") or services.get("arbitrage_script", "")
    if not script:
        return CheckResult(name, WARN, "aucun script configuré (services.arbitrage_script vide)",
                           hint="Renseigner services.arbitrage_script, ou utiliser un backend déjà lancé.")
    if not is_file(script):
        return CheckResult(
            name, FAIL, f"introuvable : {script}",
            hint="Adapter services.arbitrage_script au chemin réel (le script livré est un EXEMPLE machine-spécifique).",
        )
    if not is_executable(script):
        return CheckResult(name, WARN, f"présent mais non exécutable : {script}",
                           hint=f"chmod +x {script}")
    return CheckResult(name, OK, f"présent et exécutable : {script}")


def check_arbitrage_llm(
    cfg: dict,
    *,
    probe: Callable[[int], dict | None] | None = None,
) -> CheckResult:
    name = "LLM d'arbitrage (serveur)"
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
            name, WARN, f"aucun serveur ne répond sur le port {port} (lancé à la demande)",
            hint=f"S'il reste « down » après lancement, lire {log_path} et ./scripts/check_arbitrage_llm.sh.",
        )
    active = ""
    data = models.get("data") or []
    if data:
        active = data[0].get("id", "")
    if expected_model and active and active != expected_model:
        return CheckResult(
            name, WARN, f"actif sur le port {port} mais modèle « {active} » ≠ services.arbitrage_api_model_id « {expected_model} »",
            hint="Aligner services.arbitrage_api_model_id sur l'alias réellement servi.",
        )
    return CheckResult(name, OK, f"répond sur le port {port}" + (f" (modèle « {active} »)" if active else ""))


def check_opencode(
    cfg: dict,
    *,
    finder: Callable[..., str | None] | None = None,
) -> CheckResult:
    name = "Binaire opencode (phases LLM)"
    workflow = cfg.get("workflow", {})
    summary_on = workflow.get("summary_llm", {}).get("enabled", False)
    arbitration_on = workflow.get("arbitration_llm", {}).get("enabled", False)
    if not (summary_on or arbitration_on):
        return CheckResult(name, OK, "phases LLM désactivées — opencode non requis")

    config_bin = workflow.get("arbitration_llm", {}).get("opencode_bin")
    if finder is None:
        from transcria.gpu.opencode_setup import find_opencode_binary

        finder = find_opencode_binary
    resolved = finder(config_bin=config_bin)
    if not resolved:
        return CheckResult(
            name, FAIL, "opencode introuvable (PATH, TRANSCRIA_OPENCODE_BIN, chemins connus)",
            hint="Installer opencode et/ou définir TRANSCRIA_OPENCODE_BIN — cf. docs/INSTALL.md.",
        )
    return CheckResult(name, OK, f"trouvé : {resolved}")


def check_inference_nodes(
    cfg: dict,
    *,
    health: Callable[[str], bool] | None = None,
) -> CheckResult:
    name = "Nœud(s) de ressources distant(s)"
    inference = cfg.get("inference", {})
    mode = inference.get("mode", "local")
    if mode == "local":
        return CheckResult(name, OK, "topologie locale — pas de nœud distant à joindre")

    nodes = inference.get("nodes") or []
    urls = [n.get("url", "") for n in nodes if n.get("url")] if nodes else []
    if not urls and inference.get("url"):
        urls = [inference["url"]]
    if not urls:
        return CheckResult(name, WARN, f"mode « {mode} » mais aucun nœud configuré (inference.nodes / inference.url)",
                           hint="Renseigner inference.nodes (ou inference.url), ou repasser inference.mode à local.")

    health = health or _probe_node_health
    reachable = [u for u in urls if _safe_health(health, u)]
    fallback = bool(inference.get("fallback_local", True))
    if reachable:
        return CheckResult(name, OK, f"{len(reachable)}/{len(urls)} nœud(s) joignable(s) : {', '.join(reachable)}")
    if fallback:
        return CheckResult(name, WARN, f"aucun des {len(urls)} nœud(s) ne répond — repli local actif (mode dégradé)",
                           hint="Vérifier le service distant ; sinon les jobs basculent en local.")
    return CheckResult(name, FAIL, f"aucun des {len(urls)} nœud(s) ne répond et fallback_local=false",
                       hint="Démarrer le nœud de ressources, ou activer inference.fallback_local.")


def check_storage(
    cfg: dict,
    *,
    is_writable: Callable[[str], bool] | None = None,
) -> CheckResult:
    name = "Dossiers de travail (inscriptibles)"
    is_writable = is_writable or _dir_writable
    targets: list[tuple[str, str]] = []
    jobs_dir = cfg.get("storage", {}).get("jobs_dir", "./jobs")
    targets.append(("storage.jobs_dir", jobs_dir))
    voice = cfg.get("voice_enrollment", {})
    if voice.get("enabled"):
        targets.append(("voice_enrollment.storage_dir", voice.get("storage_dir", "./voices")))

    failures = [f"{label} ({path})" for label, path in targets if not is_writable(path)]
    if failures:
        return CheckResult(name, FAIL, "non inscriptible : " + "; ".join(failures),
                           hint="Créer le dossier et corriger les droits (l'utilisateur du service doit pouvoir écrire).")
    return CheckResult(name, OK, ", ".join(f"{label}={path}" for label, path in targets) + " inscriptibles")


# ── Sondes / helpers effectifs (injectables ci-dessus) ────────────────────


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


def _redact_uri(uri: str) -> str:
    try:
        from sqlalchemy.engine.url import make_url

        return make_url(uri).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001
        return uri


# ── Orchestration / rendu ─────────────────────────────────────────────────

_CHECKS: tuple[Callable[[dict], CheckResult], ...] = (
    check_database,
    check_arbitrage_script,
    check_arbitrage_llm,
    check_opencode,
    check_inference_nodes,
    check_storage,
)


def run_doctor(config_path: str | None = None, *, loader: Callable[..., dict] | None = None) -> list[CheckResult]:
    """Charge la config puis exécute toutes les vérifications. La config illisible
    court-circuite (un seul ``fail``, le reste dépend d'elle)."""
    if loader is None:
        from transcria.config.loader import load_config

        loader = load_config
    try:
        cfg = loader(config_path)
    except Exception as exc:  # noqa: BLE001
        return [CheckResult("Configuration", FAIL, f"chargement impossible : {exc}",
                            hint="Corriger la syntaxe YAML de config.yaml (cf. config.example.yaml).")]

    path_used = config_path or os.environ.get("TRANSCRIA_CONFIG") or "config.yaml"
    results = [CheckResult("Configuration", OK, f"chargée ({path_used})")]
    for check in _CHECKS:
        try:
            results.append(check(cfg))
        except Exception as exc:  # noqa: BLE001 — une vérif ne doit jamais crasher le doctor
            results.append(CheckResult(getattr(check, "__name__", "check"), FAIL, f"vérification en erreur : {exc}"))
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

    lines = ["", "TranscrIA doctor — préflight de diagnostic", "=" * 44]
    for r in results:
        col = ansi.get(r.status, "")
        lines.append(f"{col}{_SYMBOLS[r.status]} [{_LABELS[r.status]:>4}]{reset} {r.name} — {r.detail}")
        if r.hint and r.status != OK:
            lines.append(f"          ↳ {r.hint}")

    n_fail = sum(1 for r in results if r.status == FAIL)
    n_warn = sum(1 for r in results if r.status == WARN)
    n_ok = sum(1 for r in results if r.status == OK)
    lines.append("-" * 44)
    lines.append(f"Bilan : {n_ok} OK, {n_warn} avertissement(s), {n_fail} échec(s).")
    if n_fail:
        lines.append("→ Des problèmes bloquants ont été détectés (voir ↳).")
    elif n_warn:
        lines.append("→ Aucun échec bloquant ; vérifier les avertissements.")
    else:
        lines.append("→ Tout est vert.")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="transcria doctor",
        description="Préflight de diagnostic GPU-free : config, schéma DB, LLM d'arbitrage, opencode, "
                    "nœuds distants, dossiers de travail.",
    )
    parser.add_argument("--config", default=None, help="chemin de config.yaml (défaut : TRANSCRIA_CONFIG ou ./config.yaml)")
    parser.add_argument("--strict", action="store_true", help="traiter les avertissements comme des échecs (code de sortie ≠ 0)")
    parser.add_argument("--json", action="store_true", help="sortie JSON (pour l'outillage / CI)")
    args = parser.parse_args(argv)

    results = run_doctor(config_path=args.config)
    if args.json:
        import json

        print(json.dumps([r.__dict__ for r in results], ensure_ascii=False, indent=2))
    else:
        print(format_report(results))
    return compute_exit_code(results, strict=args.strict)


if __name__ == "__main__":
    raise SystemExit(main())

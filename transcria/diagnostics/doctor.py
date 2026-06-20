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

OK = "ok"
WARN = "warn"
FAIL = "fail"

_SYMBOLS = {OK: "✓", WARN: "⚠", FAIL: "✗"}
_LABELS = {OK: "OK", WARN: "WARN", FAIL: "FAIL"}

EXIT_OK = 0
EXIT_FAIL = 1

_VALID_PROFILES = ("all-in-one", "web", "scheduler", "resource-node", "migrate")

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
    name = "Base de données (encodage)"
    uri = database_uri or _resolve_database_uri(cfg)
    if not uri.startswith("postgresql"):
        return CheckResult(name, OK, "SQLite — encodage géré par le fichier, rien à vérifier")
    probe = prober or _probe_server_encoding
    try:
        encoding = str(probe(uri)).upper()
    except Exception as exc:  # noqa: BLE001 — toute panne de connexion = fail explicite
        return CheckResult(
            name, FAIL, f"base injoignable ({_redact_uri(uri)}) : {exc}",
            hint="Vérifier que la base tourne et que TRANSCRIA_DATABASE_URL / storage.database_url est correct.",
        )
    if encoding == "UTF8":
        return CheckResult(name, OK, "PostgreSQL en UTF8")
    return CheckResult(
        name, WARN,
        f"PostgreSQL en {encoding} (UTF8 attendu) — texte stocké sans validation d'encodage",
        hint="Migrer la base (dump → CREATE DATABASE … ENCODING 'UTF8' TEMPLATE template0 → restore), "
             "cf. docs/INSTALL.md § Encodage. L'app force client_encoding=utf8 en attendant.",
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
    name = "Résolution du modèle opencode (provider local)"
    workflow = cfg.get("workflow", {})
    summary_on = workflow.get("summary_llm", {}).get("enabled", False)
    arbitration_on = workflow.get("arbitration_llm", {}).get("enabled", False)
    if not (summary_on or arbitration_on):
        return CheckResult(name, OK, "phases LLM désactivées — résolution non requise")

    model_id = ((workflow.get("arbitration_llm", {}) or {}).get("model_id") or "").strip()
    if not model_id:
        return CheckResult(
            name, WARN, "workflow.arbitration_llm.model_id non défini",
            hint="Définir model_id (ex. local/arbitrage) dans config.yaml.",
        )
    if "/" not in model_id:
        return CheckResult(
            name, WARN, f"model_id « {model_id} » sans provider (attendu provider/modèle, ex. local/arbitrage)",
            hint="opencode résout les modèles sous la forme provider/modèle.",
        )
    provider, _, model_key = model_id.partition("/")

    path = config_path or _opencode_config_path()
    reader = reader or _read_opencode_config
    data = reader(path)
    if data is None:
        return CheckResult(
            name, FAIL, f"config opencode introuvable/illisible : {path}",
            hint="Lancer scripts/setup_opencode.py (génère provider.local d'après config.yaml).",
        )
    providers = data.get("provider") or {}
    prov = providers.get(provider)
    if not isinstance(prov, dict) or not isinstance(prov.get("models"), dict):
        return CheckResult(
            name, FAIL, f"provider opencode « {provider} » absent (ou sans modèles) dans {path}",
            hint=f"Lancer scripts/setup_opencode.py (écrit provider.{provider} selon config.yaml).",
        )
    models = prov["models"]
    if model_key not in models:
        available = ", ".join(models) or "(aucun)"
        return CheckResult(
            name, FAIL,
            f"opencode {provider} n'expose pas le modèle « {model_key} » (présents : {available}) — "
            f"« {model_id} » ne se résout pas → phases LLM en « aucun texte produit »",
            hint="Réaligner avec scripts/setup_opencode.py (régénère provider.local d'après "
                 "workflow.arbitration_llm.model_id — contrat d'alias générique).",
        )
    return CheckResult(name, OK, f"« {model_id} » résout (provider {provider}, modèle {model_key})")


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
    name = "Production LLM (opencode smoke)"
    workflow = cfg.get("workflow", {})
    if not (workflow.get("summary_llm", {}).get("enabled", False)
            or workflow.get("arbitration_llm", {}).get("enabled", False)):
        return CheckResult(name, OK, "phases LLM désactivées — smoke non requis")

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
            name, FAIL, f"LLM d'arbitrage injoignable sur le port {port} — smoke non lancé",
            hint=f"Lancez-la d'abord (./scripts/launch_arbitrage.sh) puis relancez ; en cas d'échec de démarrage, lire {log_path}.",
        )

    if runner_factory is None:
        from transcria.gpu.opencode_runner import OpenCodeRunner

        runner_factory = OpenCodeRunner

    try:
        timeout_s = int(workflow.get("arbitration_llm", {}).get("smoke_timeout_seconds", 120))
    except (TypeError, ValueError):
        timeout_s = 120

    with tempfile.TemporaryDirectory(prefix="transcria_doctor_smoke_") as tmp:
        work = Path(tmp)
        prompt_file = work / "smoke_prompt.txt"
        prompt_file.write_text("Tu es un assistant de test de diagnostic. Suis exactement la consigne.", encoding="utf-8")
        runner = runner_factory(str(work), config=cfg)
        result = runner.run(
            "Écris exactement le texte « OK » dans un fichier nommé smoke.md, sans rien d'autre.",
            str(prompt_file),
            timeout=timeout_s,
        )
        if not result.get("success"):
            return CheckResult(
                name, FAIL, f"opencode a échoué : {result.get('error', 'inconnu')}",
                hint=f"Vérifier la LLM d'arbitrage (port {port}) et lire {log_path}.",
            )
        smoke = work / "smoke.md"
        produced = bool(result.get("output")) or (
            smoke.is_file() and bool(smoke.read_text(encoding="utf-8").strip())
        )
        if not produced:
            return CheckResult(
                name, FAIL,
                "opencode a terminé (exit 0) mais la LLM n'a produit AUCUN texte ni fichier",
                hint="La LLM démarre mais ne génère rien (transcript/contexte trop long, modèle ou "
                     f"prompt inadapté). Lire {log_path}.",
            )
    return CheckResult(name, OK, f"opencode → LLM → texte : production confirmée (port {port})")


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


def check_remote_stt_control_plane(cfg: dict) -> CheckResult:
    """Vérifie qu'un STT distant a aussi un nœud de contrôle pour `/engines/ensure`."""
    name = "Cohérence STT distant / nœud de contrôle"
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
        return CheckResult(name, OK, "aucun backend STT distant déclaré")
    if mode not in ("remote", "hybrid"):
        return CheckResult(
            name,
            WARN,
            f"backend(s) STT distant(s) déclaré(s) ({', '.join(remote_backends)}) mais inference.mode={mode}",
            hint="Passer inference.mode à remote/hybrid ou retirer les URLs STT distantes.",
        )

    nodes = inference.get("nodes") or []
    urls = [n.get("url", "") for n in nodes if isinstance(n, dict) and n.get("url")] if isinstance(nodes, list) else []
    if not urls and inference.get("url"):
        urls = [inference["url"]]
    if not urls:
        return CheckResult(
            name,
            WARN,
            f"backend(s) STT distant(s) déclaré(s) ({', '.join(remote_backends)}) sans inference.url / inference.nodes",
            hint="Déclarer le nœud de contrôle resource-node pour permettre /engines/ensure avant les jobs.",
        )
    return CheckResult(name, OK, f"{len(remote_backends)} backend(s) STT distant(s), {len(urls)} nœud(s) de contrôle déclaré(s)")


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
    name = "GPU des nœuds de ressources distants"
    inference = cfg.get("inference", {})
    mode = inference.get("mode", "local")
    if mode == "local":
        return CheckResult(name, OK, "topologie locale — pas de nœud distant à sonder")

    nodes = inference.get("nodes") or []
    urls = [n.get("url", "") for n in nodes if n.get("url")] if nodes else []
    if not urls and inference.get("url"):
        urls = [inference["url"]]
    if not urls:
        return CheckResult(name, OK, "aucun nœud configuré — couvert par le check de joignabilité")

    probe = capabilities_probe or _probe_node_capabilities
    reachable = [(u, caps) for u in urls if (caps := _safe_capabilities(probe, u)) is not None]
    if not reachable:
        return CheckResult(name, OK, "aucun nœud ne renvoie /capabilities — couvert par le check de joignabilité")

    without_gpu = [u for u, caps in reachable if not _caps_reports_gpu(caps)]
    if without_gpu:
        return CheckResult(
            name, WARN,
            f"{len(without_gpu)}/{len(reachable)} nœud(s) joignable(s) n'énumèrent aucun GPU : "
            + ", ".join(without_gpu),
            hint="Vérifier nvidia-smi / la configuration GPU du nœud distant ; sinon les jobs "
                 "distants défèrent au pré-vol (admission VRAM impossible) au lieu d'être dispatchés.",
        )
    return CheckResult(name, OK, f"{len(reachable)} nœud(s) joignable(s) énumèrent leurs GPU")


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
    name = "Modèles locaux (cache)"
    exists = asset_exists or _model_asset_exists
    assets = expected_model_assets(cfg)
    if not assets:
        return CheckResult(name, OK, "aucun modèle local requis (phases servies à distance)")
    missing = [(label, ref) for label, kind, ref in assets if not exists(kind, ref)]
    if not missing:
        return CheckResult(name, OK, f"{len(assets)} modèle(s) requis présents en cache local")
    return CheckResult(
        name, WARN,
        "absent(s) du cache local : " + "; ".join(f"{label} ({ref})" for label, ref in missing),
        hint="Pré-télécharger depuis une session disposant du réseau (proxy d'entreprise compris), "
             "ex. `huggingface-cli download <id>` — cf. docs/INSTALL.md § « Réseau d'entreprise ». "
             "Sinon le premier job tentera le téléchargement et échouera si la sortie réseau est bloquée.",
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
    name = "Stockage des fichiers de jobs (split)"
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
                name, FAIL, "shared_backend=pg mais la base n'est pas PostgreSQL",
                hint="Le backend pg réplique les fichiers via PostgreSQL : corriger storage.database_url.",
            )
        probe = table_exists or _job_files_table_exists
        try:
            ready = probe(str(url))
        except Exception as exc:  # noqa: BLE001 — panne de connexion = fail explicite
            return CheckResult(
                name, FAIL, f"backend pg : base injoignable ({exc})",
                hint="Vérifier que PostgreSQL tourne et que le DSN est correct.",
            )
        if not ready:
            return CheckResult(
                name, FAIL, "backend pg : tables job_files absentes",
                hint="Appliquer `alembic upgrade head` (ou démarrer l'app, qui les crée via create_all).",
            )
        return CheckResult(
            name, OK,
            "backend pg : fichiers répliqués via PostgreSQL (tables job_files présentes)",
        )
    if role in ("web", "scheduler"):
        return CheckResult(
            name, WARN,
            f"role={role} avec shared_backend=fs : exige un jobs_dir PARTAGÉ entre frontale et worker",
            hint="Deux machines sans montage commun → passer storage.shared_backend: pg "
                 "(cf. docs/STOCKAGE_PARTAGE_JOBS.md) ; même machine ou NFS → OK, ignorer.",
        )
    return CheckResult(name, OK, "tout-en-un (fs) : disque local suffisant")


def check_deployment_profile(cfg: dict, *, profile: str | None = None) -> CheckResult:
    """Valide les invariants de haut niveau du profil d'installation demandé.

    Ce check ne remplace pas les vérifications spécialisées (DB, stockage, nœuds),
    il vérifie que le rôle runtime et le type de base ne contredisent pas le profil
    annoncé par l'installateur.
    """
    name = "Profil de déploiement"
    if not profile:
        role = _effective_runtime_role(cfg)
        return CheckResult(name, OK, f"aucun profil doctor forcé (runtime.role={role})")
    if profile not in _VALID_PROFILES:
        return CheckResult(
            name, FAIL, f"profil inconnu : {profile}",
            hint="Profils attendus : " + ", ".join(_VALID_PROFILES),
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
            name, FAIL, f"--profile {profile} mais runtime effectif={role} (attendu {expected_role})",
            hint="Corriger runtime.role dans config.yaml ou TRANSCRIA_ROLE dans .env/environnement.",
        )
    if profile in ("web", "scheduler", "migrate") and not is_postgres:
        return CheckResult(
            name, FAIL, f"--profile {profile} exige PostgreSQL ({_redact_uri(uri)})",
            hint="Définir TRANSCRIA_DATABASE_URL ou storage.database_url avec un DSN postgresql+psycopg://...",
        )
    if profile == "resource-node":
        return CheckResult(name, OK, "resource-node : service GPU dédié, base applicative non requise")
    return CheckResult(name, OK, f"--profile {profile} cohérent (runtime.role={role})")


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
    name = "Services systemd (profil)"
    if not profile:
        return CheckResult(name, OK, "aucun profil doctor forcé")
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
                detail.append("actif")
            if enabled:
                detail.append("activé")
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
            f"--profile {profile} avec service(s) incompatible(s) détecté(s) : " + "; ".join(conflicts),
            hint="Désactiver les services incompatibles avant démarrage (ex. `sudo systemctl disable --now transcria.service`).",
        )
    if not saw_systemd:
        return CheckResult(name, OK, "systemd non disponible ou unités absentes — check de conflit sauté")
    return CheckResult(name, OK, f"--profile {profile} : aucun conflit systemd détecté")


def check_resource_node_auth(cfg: dict) -> CheckResult:
    """Un nœud de ressources exposé doit avoir une clé API configurée.

    L'application autorise le mode ouvert pour le développement local, mais le profil
    `resource-node` correspond à un service réseau appelé par une frontale distante :
    le doctor doit rendre l'oubli visible.
    """
    name = "Nœud de ressources (auth API)"
    auth = ((cfg.get("inference") or {}).get("auth") or {})
    env_name = str(auth.get("api_key_env") or "TRANSCRIA_INFERENCE_API_KEY")
    env_value = os.environ.get(env_name)
    direct = auth.get("api_key")
    if env_value or direct:
        source = f"env {env_name}" if env_value else "config inference.auth.api_key"
        return CheckResult(name, OK, f"clé API configurée ({source})")
    return CheckResult(
        name,
        FAIL,
        f"aucune clé API configurée ({env_name} absent et inference.auth.api_key vide)",
        hint=f"Ajouter {env_name}=<secret long> dans .env ou renseigner inference.auth.api_key.",
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
    name = "Nœud de ressources (moteurs STT)"
    engines = ((cfg.get("resource_node") or {}).get("engines") or [])
    if not engines:
        return CheckResult(
            name,
            WARN,
            "aucun moteur STT déclaré dans resource_node.engines",
            hint="Déclarer les moteurs STT attendus (ex. cohere/whisper) ou ignorer si ce nœud ne sert que diarize/voice-embed.",
        )
    if not isinstance(engines, list):
        return CheckResult(name, FAIL, "resource_node.engines doit être une liste")

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
            errors.append(f"entrée #{index} invalide (objet attendu)")
            continue
        label = str(raw.get("name") or f"#{index}")
        missing = [key for key in ("name", "script", "gpu", "port") if raw.get(key) in (None, "")]
        if missing:
            errors.append(f"{label}: champ(s) requis manquant(s): {', '.join(missing)}")
            continue

        engine_name = str(raw["name"]).strip()
        if engine_name in seen_names:
            errors.append(f"{engine_name}: nom de moteur dupliqué")
        seen_names.add(engine_name)

        try:
            port = int(raw["port"])
            if port < 1 or port > 65535:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"{engine_name}: port invalide ({raw.get('port')!r})")
            continue
        if port in seen_ports:
            errors.append(f"{engine_name}: port dupliqué ({port})")
        seen_ports.add(port)
        if port in reserved_ports:
            errors.append(f"{engine_name}: port réservé au service inference_service ({port})")

        try:
            gpu = int(raw["gpu"])
            if gpu < 0:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"{engine_name}: gpu invalide ({raw.get('gpu')!r})")

        try:
            gpu_mem = float(raw.get("gpu_mem", 0.85))
            if gpu_mem <= 0 or gpu_mem > 1:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"{engine_name}: gpu_mem invalide ({raw.get('gpu_mem')!r}, attendu 0 < valeur <= 1)")

        script = _resolve_manifest_path(str(raw["script"]))
        if not is_file(script):
            errors.append(f"{engine_name}: script introuvable ({script})")
        elif not is_executable(script):
            warnings.append(f"{engine_name}: script présent mais non exécutable ({script})")

    if errors:
        return CheckResult(
            name,
            FAIL,
            "; ".join(errors),
            hint="Corriger resource_node.engines dans config.yaml avant d'utiliser /engines/ensure.",
        )
    if warnings:
        return CheckResult(
            name,
            WARN,
            "; ".join(warnings),
            hint="Rendre les scripts exécutables (`chmod +x scripts/launch_stt_*.sh`) pour garder un manifeste propre.",
        )
    return CheckResult(name, OK, f"{len(engines)} moteur(s) STT déclaré(s) cohérent(s)")


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
    name = "Nœud de ressources (ports STT)"
    engines = ((cfg.get("resource_node") or {}).get("engines") or [])
    if not isinstance(engines, list) or not engines:
        return CheckResult(name, OK, "aucun port STT déclaré à sonder")

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
            active = str((models.get("data") or [{}])[0].get("id") or "modèle inconnu")
            occupied_by_engine.append(f"{engine_name}:{port} ({active})")
        else:
            conflicts.append(f"{engine_name}:{port}")

    if conflicts:
        return CheckResult(
            name,
            FAIL,
            "port(s) STT occupé(s) par un service non OpenAI-compatible : " + ", ".join(conflicts),
            hint="Libérer ces ports ou modifier resource_node.engines avant de démarrer les moteurs STT.",
        )
    details = []
    if free_ports:
        details.append("libres: " + ", ".join(free_ports))
    if occupied_by_engine:
        details.append("déjà actifs: " + ", ".join(occupied_by_engine))
    return CheckResult(name, OK, "; ".join(details) if details else "ports STT cohérents")


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

_CHECKS: tuple[Callable[[dict], CheckResult], ...] = (
    check_database,
    check_database_encoding,
    check_arbitrage_script,
    check_arbitrage_llm,
    check_opencode,
    check_opencode_model_resolution,
    check_inference_nodes,
    check_remote_stt_control_plane,
    check_inference_node_gpus,
    check_local_models,
    check_storage,
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
        from transcria.config.loader import load_config

        loader = load_config
    try:
        cfg = loader(config_path)
    except Exception as exc:  # noqa: BLE001
        return [CheckResult("Configuration", FAIL, f"chargement impossible : {exc}",
                            hint="Corriger la syntaxe YAML de config.yaml (cf. config.example.yaml).")]

    path_used = config_path or os.environ.get("TRANSCRIA_CONFIG") or "config.yaml"
    results = [CheckResult("Configuration", OK, f"chargée ({path_used})")]
    if profile:
        results.append(check_deployment_profile(cfg, profile=profile))
        results.append(check_systemd_profile(cfg, profile=profile))
    checks = _checks_for_profile(profile)
    checks = (*checks, check_opencode_smoke) if llm_smoke else checks
    for check in checks:
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
    parser.add_argument("--profile", choices=_VALID_PROFILES, default=None,
                        help="profil de déploiement à valider (all-in-one|web|scheduler|resource-node|migrate)")
    parser.add_argument("--strict", action="store_true", help="traiter les avertissements comme des échecs (code de sortie ≠ 0)")
    parser.add_argument("--json", action="store_true", help="sortie JSON (pour l'outillage / CI)")
    parser.add_argument("--llm-smoke", action="store_true",
                        help="ajoute un test RÉEL opencode→LLM→texte (nécessite la LLM up + VRAM ; non GPU-free)")
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

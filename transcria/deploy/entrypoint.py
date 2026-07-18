"""Entrypoint Docker par rôle : `python -m transcria.deploy.entrypoint <role>`.

Invariants P5 (cf. docs/archive/PLAN_EVOLUTION_INSTALLATION.md § P5) :
  * mêmes profils que l'install (`web`, `scheduler`, `resource-node`, `migrate`) ;
  * `install.sh` n'est JAMAIS l'entrypoint applicatif — l'image est déjà construite,
    le runtime ne fait que provisionner puis exec la commande du rôle ;
  * PostgreSQL obligatoire (SQLite refusé) pour les rôles à base applicative ;
  * `migrate` est un job one-shot (`alembic upgrade head`) ; les rôles serveurs
    attendent que la base soit joignable puis démarrent (pas de migration implicite —
    elle est jouée par le job `migrate` dédié).

Le process est **remplacé** (`os.execvp`) pour que le serveur hérite de PID 1 et
reçoive proprement les signaux du conteneur. Sonde DB et exec sont injectables (tests).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from transcria.installer.i18n_phase import I18nPlan, apply_i18n
from transcria.installer.postgres_phase import _default_query

# Rôles conteneurisables et ceux qui exigent une base applicative PostgreSQL.
#   all          : tout-en-un (UI web + scheduler + inférence GPU in-process) — le plus
#                  simple pour tester le projet dans un seul conteneur ;
#   web/scheduler: déploiement distribué (split) ;
#   resource-node: nœud GPU pur (STT/diarisation), sans base applicative ;
#   migrate      : job one-shot Alembic.
ROLES = ("all", "web", "scheduler", "resource-node", "migrate")
_DB_ROLES = ("all", "web", "scheduler", "migrate")  # resource-node = nœud GPU pur, sans base applicative

ExecFn = Callable[[str, Sequence[str]], None]
DbProbe = Callable[[str], bool]


@dataclass(frozen=True)
class EntrypointPlan:
    role: str
    config_path: Path
    database_url: str = ""
    bind: str = "0.0.0.0:7870"
    inference_bind: str = "0.0.0.0:8002"
    workers: int = 4
    threads: int = 4
    timeout: int = 120
    python: str = field(default_factory=lambda: sys.executable)
    gunicorn: str = "gunicorn"
    alembic: str = "alembic"
    app_module: str = "app.py"


def needs_database(role: str) -> bool:
    return role in _DB_ROLES


def _split_bind(bind: str) -> tuple[str, str]:
    """'host:port' → (host, port). Tolère un bind sans port."""
    host, _, port = bind.rpartition(":")
    return (host or "0.0.0.0", port or "7870")


def build_role_command(plan: EntrypointPlan) -> list[str]:
    """Commande de lancement du rôle (fidèle aux unités systemd `deploy/`)."""
    if plan.role == "all":
        # Tout-en-un : app.py rôle 'all' (UI + scheduler + inférence in-process). Adapté
        # à un test mono-conteneur ; un GPU est requis pour le traitement réel des jobs.
        host, port = _split_bind(plan.bind)
        return [plan.python, plan.app_module, "--role", "all", "--host", host, "--port", port]
    if plan.role == "web":
        return [
            plan.gunicorn,
            "--workers", str(plan.workers),
            "--bind", plan.bind,
            "--timeout", str(plan.timeout),
            "--access-logfile", "-",
            "--error-logfile", "-",
            "wsgi:app",
        ]
    if plan.role == "scheduler":
        return [plan.python, plan.app_module, "--role", "scheduler"]
    if plan.role == "resource-node":
        return [
            plan.gunicorn,
            "inference_service:create_app()",
            "-b", plan.inference_bind,
            "--workers", "1",
            "--threads", str(plan.threads),
            "--timeout", str(plan.timeout),
            "--access-logfile", "-",
            "--error-logfile", "-",
        ]
    if plan.role == "migrate":
        return [plan.alembic, "upgrade", "head"]
    raise ValueError(f"rôle inconnu : {plan.role}")


def preflight(plan: EntrypointPlan) -> list[str]:
    """Vérifie les invariants conteneur. Retourne la liste des erreurs actionnables."""
    errors: list[str] = []
    if plan.role not in ROLES:
        errors.append(f"rôle inconnu '{plan.role}' (attendus : {', '.join(ROLES)})")
        return errors

    if not plan.config_path.is_file():
        errors.append(
            f"config.yaml introuvable ({plan.config_path}) — monter le volume de configuration "
            "(ex. -v ./config.yaml:/app/config.yaml) ou définir TRANSCRIA_CONFIG."
        )

    if needs_database(plan.role):
        dsn = plan.database_url.strip()
        if not dsn:
            errors.append(
                "TRANSCRIA_DATABASE_URL requis : PostgreSQL est obligatoire en conteneur "
                f"pour le rôle '{plan.role}' (SQLite n'est pas un mode de déploiement Docker supporté)."
            )
        elif dsn.startswith("sqlite"):
            errors.append(
                f"SQLite refusé en conteneur (rôle '{plan.role}') : fournir un DSN PostgreSQL "
                "dans TRANSCRIA_DATABASE_URL."
            )
    return errors


def _default_exec(file: str, args: Sequence[str]) -> None:
    os.execvp(file, list(args))  # remplace le process (PID 1 hérite des signaux)


def _default_db_probe(database_url: str) -> bool:
    # Réutilise la sonde de la phase PostgreSQL (SQLAlchemy/psycopg, sans dépendre de psql).
    return _default_query(database_url, "SELECT 1") == "1"


def wait_for_database(
    database_url: str,
    *,
    probe: DbProbe,
    attempts: int = 30,
    delay: float = 1.0,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> bool:
    """Attend que la base réponde (compose : la DB peut démarrer après le service)."""
    for attempt in range(1, attempts + 1):
        if probe(database_url):
            return True
        if attempt < attempts:
            sleep_fn(delay)
    return False


def classify_db_unreachable(database_url: str) -> str:
    """Message ACTIONNABLE quand la base reste inaccessible — distingue AUTH de réseau.

    `wait_for_database` ne renvoie qu'un booléen (la sonde avale l'exception), donc une
    authentification refusée ressemblait à tort à « injoignable ». À l'échec, on rejoue UNE
    connexion pour capturer la cause et guider l'opérateur — en particulier le piège fréquent :
    un VOLUME PostgreSQL PRÉEXISTANT créé avec un autre `POSTGRES_PASSWORD` (la base garde le
    mot de passe d'INITIALISATION du volume, pas celui de l'environnement courant) → l'auth TCP
    échoue alors que le service répond. Retourne "" si la base est en fait joignable (course).
    """
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(database_url)
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return ""
        finally:
            engine.dispose()
    except Exception as exc:  # noqa: BLE001 — on classe le message, on ne propage pas
        msg = str(exc).lower()
        if "password authentication failed" in msg or "authentification" in msg:
            return (
                "AUTHENTIFICATION refusée (mot de passe). Cause la plus fréquente : un VOLUME "
                "PostgreSQL PRÉEXISTANT a été initialisé avec un autre POSTGRES_PASSWORD — la base "
                "conserve le mot de passe de CRÉATION du volume, pas celui de l'environnement actuel. "
                "Corriger par l'une des options : (a) remettre le mot de passe d'origine dans "
                "POSTGRES_PASSWORD ; (b) réinitialiser le volume — `docker compose down -v` "
                "(⚠ EFFACE les données) puis relancer ; (c) changer le mot de passe du rôle sans "
                "perdre les données : `docker compose exec db psql -U <user> -d <db> -c "
                "\"ALTER USER <user> WITH PASSWORD '<nouveau>';\"`."
            )
        if "could not translate host" in msg or "name or service not known" in msg or "nodename nor servname" in msg:
            return (
                "HÔTE introuvable (DNS). Vérifier l'hôte dans TRANSCRIA_DATABASE_URL : en compose "
                "c'est le nom du service (`db`), pas `127.0.0.1` (qui désigne le conteneur lui-même)."
            )
        if "connection refused" in msg:
            return "connexion REFUSÉE — PostgreSQL n'écoute pas encore (service non démarré ou mauvais port)."
        return f"erreur de connexion : {exc}"


# Rôles qui exécutent les phases LLM (correction/résumé via opencode) → provisioning opencode.
_LLM_ROLES = ("all", "scheduler")
_UI_ROLES = ("all", "web")  # rôles qui servent l'interface HTML → besoin des catalogues .mo


def provision_opencode(plan: EntrypointPlan, env: dict[str, str]) -> None:
    """(Re)configure le provider `local` d'opencode depuis la config MONTÉE au runtime.

    En déploiement Docker, install.sh installe le binaire opencode au BUILD mais fige le
    `base_url` du provider sur la config de build (souvent 127.0.0.1). En topologie split,
    l'endpoint LLM est distant : on réécrit `opencode.json` à partir de la config réellement
    montée (`services.arbitrage_llm_host/port`). Sans ce provider correct, les phases LLM
    échouent (« opencode introuvable » / mauvais endpoint).

    Best-effort et idempotent : tout échec est journalisé sans bloquer le démarrage (un rôle
    sans opencode reste légitime). Corrige aussi l'all-in-one Docker, sans toucher l'install
    hôte. N'agit que pour les rôles `_LLM_ROLES`.
    """
    if plan.role not in _LLM_ROLES:
        return
    try:
        # Différés §8.3(c) : point d'entrée best-effort — un échec d'import (image slim,
        # dépendance absente) produit le WARN lisible ci-dessous, jamais un crash du rôle.
        from transcria.config import load_config
        from transcria.gpu import opencode_setup
        from transcria.workflow import agent_workspace

        cfg = load_config()
        base_url = opencode_setup.default_base_url(cfg)
        llm = (cfg.get("workflow", {}) or {}).get("arbitration_llm", {}) or {}
        model = llm.get("model_id") or "arbitrage"
        if "/" in model:  # "local/arbitrage" → clé modèle "arbitrage"
            model = model.split("/", 1)[1]
        config_path = env.get("OPENCODE_CONFIG") or os.path.expanduser(
            os.path.join("~", ".config", "opencode", "opencode.json")
        )
        opencode_setup.ensure_local_provider(config_path, base_url, model)
        # Politique headless : `external_directory` déterministe (allow sur l'arbre de scratch
        # des agents, deny ailleurs) — sinon le défaut opencode `ask` suspend `opencode run`.
        work_root = agent_workspace.resolve_agent_work_root(cfg)
        opencode_setup.ensure_agent_permissions(config_path, work_root)
        print(f"[INFO] opencode provider 'local' → {base_url} ({config_path}) "
              f"| external_directory allow={work_root}", file=sys.stderr, flush=True)
    except Exception as exc:  # noqa: BLE001 — provisioning best-effort, ne bloque jamais le rôle
        print(f"[WARN] provisioning opencode ignoré ({type(exc).__name__}: {exc})", file=sys.stderr, flush=True)


_MOSS_SITE_BAKED = Path("/opt/transcria-moss-site")
_MOSS_SITE_DEFAULT = Path("./runtimes/moss_site")


def provision_moss_site_link(
    baked: Path = _MOSS_SITE_BAKED, default: Path = _MOSS_SITE_DEFAULT
) -> None:
    """Symlinke le défaut config du site moss vers le site baké de l'image bundled.

    Le site Transformers 5 isolé est baké à un chemin STABLE (/opt). Le défaut de
    ``moss.moss_site`` étant ./runtimes/moss_site (persistant depuis 0.3.8 —
    l'ancien défaut /tmp était purgé au reboot), on pose le lien à CHAQUE
    démarrage (idempotent) :
    une config non modifiée trouve le site sans réglage. Image slim (site absent)
    ou chemin déjà occupé par un vrai site ⇒ no-op. Best-effort, ne bloque jamais.
    """
    try:
        if not baked.is_dir():
            return
        if default.is_symlink():
            if default.resolve() == baked.resolve():
                return
            default.unlink()
        elif default.exists():
            return  # un site réel existe déjà à cet emplacement — on n'y touche pas
        default.parent.mkdir(parents=True, exist_ok=True)  # ./runtimes/ peut ne pas exister (image slim)
        default.symlink_to(baked)
        print(f"[INFO] site moss baké : {default} → {baked}", file=sys.stderr, flush=True)
    except Exception as exc:  # noqa: BLE001 — best-effort, ne bloque jamais le démarrage
        print(f"[WARN] symlink site moss ignoré ({type(exc).__name__}: {exc})", file=sys.stderr, flush=True)


def provision_translations(plan: EntrypointPlan, env: dict[str, str]) -> None:
    """Filet runtime : recompile les catalogues .mo si absents/périmés (rôles UI).

    Les .mo sont normalement bakés au build (Dockerfile). Ce filet couvre le cas d'un VOLUME
    montant/écrasant ``transcria/web/translations`` (override de traductions, patch à chaud) :
    on recompile pour que l'interface reflète les .po montés. Best-effort et idempotent : tout
    échec est journalisé, l'app retombe sur le français par défaut. N'agit que pour ``_UI_ROLES``.
    """
    if plan.role not in _UI_ROLES:
        return
    try:
        app_root = Path(__file__).resolve().parents[2]
        translations_dir = app_root / "transcria" / "web" / "translations"

        class _Stderr:
            def info(self, m: str) -> None: print(f"[INFO] {m}", file=sys.stderr, flush=True)
            def ok(self, m: str) -> None: print(f"[INFO] {m}", file=sys.stderr, flush=True)
            def warn(self, m: str) -> None: print(f"[WARN] {m}", file=sys.stderr, flush=True)
            def error(self, m: str) -> None: print(f"[ERROR] {m}", file=sys.stderr, flush=True)

        apply_i18n(I18nPlan(translations_dir=translations_dir), console=_Stderr())
    except Exception as exc:  # noqa: BLE001 — best-effort, ne bloque jamais le démarrage
        print(f"[WARN] compilation des traductions ignorée ({type(exc).__name__}: {exc})", file=sys.stderr, flush=True)


ModelDownloader = Callable[[str, str, str], None]  # (repo, filename, local_dir)


def _default_model_downloader(repo: str, filename: str, local_dir: str) -> None:
    from huggingface_hub import hf_hub_download  # import paresseux : seulement au runtime conteneur

    hf_hub_download(repo_id=repo, filename=filename, local_dir=local_dir)


def provision_arbitrage_model(
    plan: EntrypointPlan,
    env: dict[str, str],
    *,
    downloader: ModelDownloader = _default_model_downloader,
) -> bool:
    """Garantit la présence du GGUF d'arbitrage et résout le script de lancement (rôle `all`).

    L'all-in-one embarque la LLM (llama-server compilé) mais PAS les poids (build hermétique,
    gating). Au runtime on télécharge le GGUF du palier (`TRANSCRIA_LLM_TIER`, défaut « 12 » =
    Qwen3.5-9B Q5_K_M, NON gated) dans `MODELS_DIR` (volume monté → persistant), et on pointe
    `TRANSCRIA_ARBITRAGE_SCRIPT` sur le profil de palier correspondant (paramétrique via
    `LLAMA_SERVER`/`MODELS_DIR`). Idempotent.

    N'agit que pour le rôle `all` et si l'arbitrage n'est pas désactivé. Best-effort : tout échec
    est journalisé et renvoie False (le lancement LLM échouera plus tard avec un message clair) ;
    ne bloque jamais le démarrage. Renvoie True si le modèle est présent/prêt.
    """
    if plan.role != "all":
        return True
    try:
        # Différés §8.3(c) : point d'entrée best-effort — l'ImportError est un cas géré.
        from transcria.config import load_config
        from transcria.installer.tiers import get_tier_metadata
    except Exception as exc:  # noqa: BLE001 — provisioning best-effort
        print(f"[WARN] provisioning modèle d'arbitrage ignoré ({type(exc).__name__}: {exc})", file=sys.stderr, flush=True)
        return False

    cfg = load_config()
    llm = (cfg.get("workflow", {}) or {}).get("arbitration_llm", {}) or {}
    if llm.get("enabled") is False:
        print("[INFO] arbitration_llm désactivée — aucun modèle d'arbitrage à provisionner.", file=sys.stderr, flush=True)
        return True

    tier = env.get("TRANSCRIA_LLM_TIER", "12")
    try:
        meta = get_tier_metadata(tier)
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] palier LLM '{tier}' inconnu ({exc}) — provisioning modèle ignoré.", file=sys.stderr, flush=True)
        return False

    # Résout le script de lancement du palier (paramétrique) si non fourni explicitement — un
    # seul bouton (TRANSCRIA_LLM_TIER) pilote modèle ET script. Profils relatifs au dépôt
    # (transcria/deploy/entrypoint.py → racine), donc corrects hors /app aussi (tests).
    if not env.get("TRANSCRIA_ARBITRAGE_SCRIPT"):
        profiles_dir = Path(__file__).resolve().parents[2] / "scripts" / "arbitrage_profiles"
        matches = sorted(profiles_dir.glob(f"{tier}gb_*.sh"))
        if matches:
            os.environ["TRANSCRIA_ARBITRAGE_SCRIPT"] = str(matches[0])
            print(f"[INFO] script d'arbitrage (palier {tier}) → {matches[0]}", file=sys.stderr, flush=True)

    dest_dir = Path(env.get("MODELS_DIR", "/app/models")) / meta.directory
    target = dest_dir / meta.file
    if target.is_file():
        print(f"[INFO] modèle d'arbitrage déjà présent : {target}", file=sys.stderr, flush=True)
        return True

    print(f"[INFO] téléchargement du modèle d'arbitrage {meta.label} → {dest_dir} "
          f"(une fois ; {meta.repo}, non gated)…", file=sys.stderr, flush=True)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        downloader(meta.repo, meta.file, str(dest_dir))
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] téléchargement du modèle d'arbitrage échoué ({type(exc).__name__}: {exc}) — "
              "résumé/correction LLM indisponibles tant que le modèle est absent.", file=sys.stderr, flush=True)
        return False
    if target.is_file():
        print(f"[INFO] modèle d'arbitrage prêt : {target}", file=sys.stderr, flush=True)
        return True
    print(f"[WARN] modèle d'arbitrage introuvable après téléchargement : {target}", file=sys.stderr, flush=True)
    return False


def _plan_from_env(role: str, env: dict[str, str]) -> EntrypointPlan:
    config_path = Path(env.get("TRANSCRIA_CONFIG", "/app/config.yaml"))
    return EntrypointPlan(
        role=role,
        config_path=config_path,
        database_url=env.get("TRANSCRIA_DATABASE_URL", ""),
        bind=env.get("TRANSCRIA_BIND", "0.0.0.0:7870"),
        inference_bind=env.get("INFERENCE_BIND", f"0.0.0.0:{env.get('INFERENCE_PORT', '8002')}"),
        workers=int(env.get("TRANSCRIA_WORKERS", "4")),
        threads=int(env.get("INFERENCE_THREADS", "4")),
    )


def main(
    argv: list[str] | None = None,
    *,
    env: dict[str, str] | None = None,
    exec_fn: ExecFn = _default_exec,
    db_probe: DbProbe | None = None,
    db_diagnoser: Callable[[str], str] | None = None,
    wait_attempts: int = 30,
    wait_delay: float = 1.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    opencode_provisioner: Callable[[EntrypointPlan, dict[str, str]], None] = provision_opencode,
    model_provisioner: Callable[[EntrypointPlan, dict[str, str]], bool] = provision_arbitrage_model,
    translations_provisioner: Callable[[EntrypointPlan, dict[str, str]], None] = provision_translations,
) -> int:
    env = dict(os.environ if env is None else env)
    probe = db_probe or _default_db_probe
    diagnoser = db_diagnoser or classify_db_unreachable

    parser = argparse.ArgumentParser(
        prog="transcria-entrypoint",
        description="Entrypoint Docker par rôle (réutilise les profils d'install, sans install.sh).",
    )
    parser.add_argument("role", nargs="?", default=env.get("TRANSCRIA_ROLE"), choices=ROLES,
                        help="Rôle du conteneur (défaut : $TRANSCRIA_ROLE)")
    parser.add_argument("--no-wait-db", action="store_true", help="Ne pas attendre la base (debug)")
    parser.add_argument("--provision-only", action="store_true",
                        help="Provisionne le modèle d'arbitrage (téléchargement) puis sort, sans démarrer "
                             "le rôle ni attendre la base (pré-téléchargement par le quickstart).")
    args = parser.parse_args(argv)

    if not args.role:
        print("[ERROR] rôle requis : argument positionnel ou $TRANSCRIA_ROLE", file=sys.stderr)
        return 2

    plan = _plan_from_env(args.role, env)

    errors = preflight(plan)
    if errors:
        for err in errors:
            print(f"[ERROR] {err}", file=sys.stderr)
        return 1

    # Pré-téléchargement seul : on ne touche ni la base ni le serveur (le gros GGUF est tiré
    # par le quickstart AVANT `up`, pour un démarrage rapide et une progression visible).
    if args.provision_only:
        return 0 if model_provisioner(plan, env) else 1

    if needs_database(plan.role) and not args.no_wait_db:
        print(f"[INFO] Attente de la base PostgreSQL (rôle {plan.role})…", file=sys.stderr, flush=True)
        if not wait_for_database(plan.database_url, probe=probe, attempts=wait_attempts, delay=wait_delay, sleep_fn=sleep_fn):
            detail = diagnoser(plan.database_url) or "vérifier TRANSCRIA_DATABASE_URL et le service de base."
            print(
                f"[ERROR] base PostgreSQL inaccessible après {wait_attempts} tentatives — {detail}",
                file=sys.stderr,
            )
            return 1

    # Filet i18n : recompile les .mo si un volume a écrasé les traductions (rôles UI).
    translations_provisioner(plan, env)
    # Image bundled : expose le site Transformers 5 baké au chemin par défaut de la config.
    provision_moss_site_link()
    # Provisionne opencode (provider local) pour les rôles LLM, depuis la config montée.
    opencode_provisioner(plan, env)
    # Provisionne le modèle d'arbitrage (rôle all : télécharge le GGUF si absent, résout le
    # script de palier). Best-effort — la valeur de retour n'interrompt pas le démarrage.
    model_provisioner(plan, env)

    cmd = build_role_command(plan)
    print(f"[INFO] Démarrage du rôle {plan.role} : {' '.join(cmd)}", file=sys.stderr, flush=True)
    exec_fn(cmd[0], cmd)
    return 0  # non atteint si exec réussit


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

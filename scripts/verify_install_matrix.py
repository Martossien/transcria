#!/usr/bin/env python3
"""Matrice ``install.sh`` × topologies dans des conteneurs **vierges** + E2E GPU/LLM.

But (operator-run, sur la machine GPU) : prouver que l'installation NATIVE via
``install.sh`` fonctionne de bout en bout sur plusieurs distributions, avec un vrai
GPU et la LLM, pour les deux topologies de déploiement :

  * **all-in-one** — un conteneur vierge : bootstrap OS → ``install.sh --profile
    all-in-one`` (torch CUDA, modèles, PostgreSQL) → service → E2E son complet
    (STT + diarisation + arbitrage LLM + export) en local sur le GPU.
  * **frontale + resource-node** — deux conteneurs : la frontale (CPU, ``--profile
    web``) délègue STT/diarisation/voix + arbitrage au nœud de ressources (GPU,
    ``--profile resource-node``). E2E son piloté via la frontale. Multi-GPU OK.

Chaque exécution : conteneur vierge AVEC accès GPU (CDI) → amorçage des prérequis OS
(``transcria.deploy.distro_bootstrap``) → ``install.sh`` → attente ``/health`` →
**assertion que le conteneur voit ET utilise le GPU** (``transcria.deploy.gpu_probe`` :
interdit le repli CPU silencieux) → **E2E son** (réutilise ``verify_split_topology.run_job``)
→ démontage. Verdict unique, échec actionnable au premier problème.

La logique de DÉCISION (specs, argv docker, rendu de config) est PURE et testée en CI
(``tests/test_verify_install_matrix.py``) ; l'exécution réelle exige Docker + GPU.

Exemple :
    TRANSCRIA_INFERENCE_API_KEY=… HF_TOKEN=… \\
    python scripts/verify_install_matrix.py --distro ubuntu2404 --topology all-in-one \\
        --audio tests/test2.mp3 --profile word_corrige
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from transcria.deploy.distro_bootstrap import available_distros, bootstrap_commands, get_distro  # noqa: E402
from transcria.deploy.gpu_probe import probe_container_gpu  # noqa: E402

# Réseau Docker dédié + noms de conteneurs stables (le nom du nœud sert d'hôte dans la
# config frontale : `inference.url = http://<NODE_NAME>:8002`).
NETWORK = "transcria-matrix-net"
NODE_NAME = "transcria-matrix-node"
APP_NAME = "transcria-matrix-app"
PG_NAME = "transcria-matrix-pg"
PG_IMAGE = "postgres:16"
CACHE_VOLUME = "transcria-matrix-cache"  # cache HF/torch persistant entre runs
CONTAINER_PYTHON = "/app/venv/bin/python"

# Secrets passés AU CONTENEUR par référence (`-e NAME`, sans valeur dans l'argv → jamais
# journalisés ni visibles dans `ps`). Lus depuis l'environnement de l'opérateur.
SECRET_ENV_PASSTHROUGH = ("HF_TOKEN", "TRANSCRIA_INFERENCE_API_KEY", "TRANSCRIA_STT_API_KEY")


# ── Specs (PUR — testé en CI) ────────────────────────────────────────────────


@dataclass(frozen=True)
class ContainerSpec:
    """Un conteneur d'une topologie."""

    name: str
    install_profile: str          # valeur de `install.sh --profile`
    config_example: str           # config.*.example.yaml servant de base
    gpu: bool                     # le conteneur reçoit-il les GPU (CDI) ?
    launch: list[str]             # commande de démarrage du service (dans /app)
    published: dict[int, int]     # port_conteneur -> port_hôte
    health_internal_port: int     # port interrogé pour /health (côté conteneur)
    needs_db: bool                # rôle à base applicative PostgreSQL (web/all) ?
    runtime_role: str | None      # TRANSCRIA_ROLE pour app.py (None = pas app.py)


@dataclass(frozen=True)
class TopologySpec:
    name: str
    description: str
    containers: tuple[ContainerSpec, ...]
    web_host_port: int            # port hôte de la frontale (pour run_job)
    gpu_container: str            # conteneur à sonder pour le GPU


def _app_launch() -> list[str]:
    # role=all (web + scheduler en process) lu depuis la config ; --no-debug en service.
    return [CONTAINER_PYTHON, "app.py", "--host", "0.0.0.0", "--port", "7870", "--no-debug"]


def _node_launch() -> list[str]:
    # Service Flask de ressources (diarize/voice-embed + supervision des moteurs STT vLLM).
    return [CONTAINER_PYTHON, "-m", "inference_service"]


def topologies() -> dict[str, TopologySpec]:
    """Catalogue des topologies testables."""
    all_in_one = TopologySpec(
        name="all-in-one",
        description="Un conteneur GPU : install.sh --profile all-in-one + E2E local complet.",
        containers=(
            ContainerSpec(
                name=APP_NAME, install_profile="all-in-one",
                config_example="config.example.yaml", gpu=True,
                launch=_app_launch(), published={7870: 7870}, health_internal_port=7870,
                needs_db=True, runtime_role="all",
            ),
        ),
        web_host_port=7870, gpu_container=APP_NAME,
    )
    frontale_split = TopologySpec(
        name="frontale-split",
        description="Frontale CPU (--profile web) + resource-node GPU (--profile resource-node).",
        containers=(
            ContainerSpec(
                name=NODE_NAME, install_profile="resource-node",
                config_example="config.resource-node.example.yaml", gpu=True,
                launch=_node_launch(), published={8002: 8002}, health_internal_port=8002,
                needs_db=False, runtime_role=None,  # nœud GPU pur (inference_service), sans base
            ),
            ContainerSpec(
                name=APP_NAME, install_profile="web",
                config_example="config.frontale.example.yaml", gpu=False,
                launch=_app_launch(), published={7870: 7870}, health_internal_port=7870,
                needs_db=True, runtime_role="all",  # web+scheduler ; inférence déportée (config remote)
            ),
        ),
        web_host_port=7870, gpu_container=NODE_NAME,
    )
    return {t.name: t for t in (all_in_one, frontale_split)}


def docker_run_argv(
    spec: ContainerSpec,
    base_image: str,
    repo_dir: Path,
    env_set: dict[str, str] | None = None,
    env_passthrough: tuple[str, ...] = (),
) -> list[str]:
    """argv de ``docker run`` pour démarrer un conteneur vierge persistant.

    Le conteneur reste en vie (``sleep infinity``) ; bootstrap/install/lancement se font
    ensuite par ``docker exec``. Le dépôt est monté en LECTURE SEULE sous /src (copié en
    /app, inscriptible, à l'install). Accès GPU via CDI seulement si ``spec.gpu``.

    ``env_set`` = variables avec valeur (DSN de test, secret de session jetable) ;
    ``env_passthrough`` = noms de secrets passés PAR RÉFÉRENCE (``-e NAME`` sans valeur →
    la valeur n'apparaît jamais dans l'argv, donc ni dans les logs ni dans ``ps``).
    """
    argv = ["docker", "run", "-d", "--name", spec.name, "--network", NETWORK]
    if spec.gpu:
        argv += ["--device", "nvidia.com/gpu=all"]
    for cport, hport in sorted(spec.published.items()):
        argv += ["-p", f"{hport}:{cport}"]
    for name, value in (env_set or {}).items():
        argv += ["-e", f"{name}={value}"]
    for name in env_passthrough:
        argv += ["-e", name]  # passe la valeur de l'hôte sans l'exposer dans l'argv
    # Cache HF/torch persistant (volume nommé) : les modèles survivent aux runs → itération
    # rapide et fidèle à la prod (pas de re-téléchargement à chaque exécution).
    argv += ["-v", f"{CACHE_VOLUME}:/root/.cache"]
    argv += ["-v", f"{repo_dir}:/src:ro", "-w", "/app", base_image, "sleep", "infinity"]
    return argv


def install_command(profile: str, cuda: str | None, pg_existing: bool, llm_backend: str | None = None) -> str:
    """Commande shell d'installation NATIVE dans le conteneur (depuis /app).

    On exerce le VRAI install (torch CUDA + modèles) : surtout PAS ``--skip-deps``.
    ``--no-service`` (pas de systemd en conteneur), ``--non-interactive`` (aucun TTY).
    Les rôles à base applicative utilisent ``--pg-existing`` : la base est fournie par un
    conteneur PostgreSQL dédié (comme le service ``db`` du déploiement Docker) → install
    écrit le DSN et joue ``alembic upgrade`` (chemin d'install réellement supporté).
    ``llm_backend`` (ollama|llamacpp) force le backend LLM en non-interactif (sinon défaut).
    """
    cmd = "cd /app && ./install.sh --profile " + profile + " --no-service --non-interactive"
    if pg_existing:
        cmd += " --pg-existing"
    if cuda:
        cmd += f" --cuda {cuda}"
    if llm_backend:
        cmd += f" --llm-backend {llm_backend}"
    return cmd


def config_setup_command(
    config_example: str, node_name: str, dsn: str | None = None, admin_password: str | None = None,
    stt_backend: str | None = None, diarization_backend: str | None = None,
) -> str:
    """Commande shell : config exemple → config.yaml, substitution NODE_IP, DSN, mot de passe admin.

    ``NODE_IP`` (placeholder des configs frontale) → nom du conteneur nœud (DNS interne du
    réseau Docker). Pour l'all-in-one (config locale sans NODE_IP), le sed est un no-op.
    ``dsn`` (rôles à base) remplace ``storage.database_url`` par la base de test externe.
    ``admin_password`` fixe ``auth.first_admin_password`` à une valeur connue → l'admin seedé
    par install.sh/ensure_admin est déterministe (sinon il reste « CHANGE-ME » de l'exemple).
    ``stt_backend``/``diarization_backend`` basculent la reconnaissance vers la voie NON gated
    (whisper + sortformer) → E2E « facile » sans token HF, cohérent avec le backend Ollama.
    """
    cmd = (
        f"cp /app/{config_example} /app/config.yaml && "
        f"sed -i 's/NODE_IP/{node_name}/g' /app/config.yaml"
    )
    if dsn:
        # `|` comme délimiteur sed : le DSN contient des `/` mais pas de `|`.
        cmd += f" && sed -i 's|database_url:.*|database_url: \"{dsn}\"|' /app/config.yaml"
    if admin_password:
        cmd += f" && sed -i 's|first_admin_password:.*|first_admin_password: \"{admin_password}\"|' /app/config.yaml"
    if stt_backend:
        cmd += f" && sed -i 's|stt_backend:.*|stt_backend: \"{stt_backend}\"|' /app/config.yaml"
    if diarization_backend:
        cmd += f" && sed -i 's|diarization_backend:.*|diarization_backend: \"{diarization_backend}\"|' /app/config.yaml"
    return cmd


# ── Exécution (subprocess) ───────────────────────────────────────────────────


def _log(stage: str, msg: str) -> None:
    print(f"[{stage}] {msg}", flush=True)


def _fail(stage: str, msg: str) -> None:
    print(f"[{stage}] ÉCHEC : {msg}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def _run(argv: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(argv, check=check, text=True,
                          capture_output=capture)


def _docker_exec(container: str, shell_cmd: str, *, detach: bool = False) -> str:
    argv = ["docker", "exec"]
    if detach:
        argv.append("-d")
    argv += [container, "bash", "-lc", shell_cmd]
    cp = _run(argv, check=not detach, capture=not detach)
    return (cp.stdout or "") if not detach else ""


def _runner(argv: list[str]) -> str:
    """Runner injecté dans gpu_probe : exécute un argv docker et renvoie stdout."""
    return subprocess.run(argv, check=True, text=True, capture_output=True).stdout


def preflight_gpu() -> None:
    """Hôte : nvidia-smi présent + Docker voit le GPU via CDI (setup_docker_gpu.sh --check)."""
    try:
        _run(["nvidia-smi", "-L"], capture=True)
    except Exception as exc:  # noqa: BLE001
        _fail("preflight", f"nvidia-smi indisponible sur l'hôte ({exc}) — pilote NVIDIA requis")
    check = _REPO / "scripts" / "setup_docker_gpu.sh"
    cp = _run(["bash", str(check), "--check"], check=False, capture=True)
    if cp.returncode != 0:
        _fail("preflight", "Docker ne voit pas le GPU (CDI). Lancer : scripts/setup_docker_gpu.sh")
    _log("preflight", "GPU hôte OK + Docker/CDI OK")


def wait_health(host_port: int, internal_port: int, container: str, timeout_s: float = 600) -> None:
    """Attend /health via le port publié sur l'hôte (boot torch/gunicorn peut être long)."""
    import requests

    url = f"http://localhost:{host_port}/health"
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        try:
            r = requests.get(url, timeout=5)
            if r.status_code == 200:
                _log("health", f"{container} /health OK ({url})")
                return
            last = f"HTTP {r.status_code}"
        except Exception as exc:  # noqa: BLE001
            last = str(exc)[:80]
        time.sleep(5)
    _fail("health", f"{container} /health KO après {timeout_s:.0f}s ({last}). Logs : docker logs {container}")


def assert_container_gpu(container: str) -> None:
    verdict = probe_container_gpu(container, _runner, python_bin=CONTAINER_PYTHON)
    if not verdict.ok:
        _fail("gpu", f"{container} : {verdict.detail}")
    _log("gpu", f"{container} : {verdict.detail}")


def teardown(names: list[str]) -> None:
    for n in [*names, PG_NAME]:
        _run(["docker", "rm", "-f", n], check=False, capture=True)
    _run(["docker", "network", "rm", NETWORK], check=False, capture=True)


def start_postgres(password: str) -> None:
    """Conteneur PostgreSQL jetable (comme le service ``db`` du déploiement Docker)."""
    _run(["docker", "run", "-d", "--name", PG_NAME, "--network", NETWORK,
          "-e", "POSTGRES_USER=transcria", "-e", f"POSTGRES_PASSWORD={password}",
          "-e", "POSTGRES_DB=transcria", PG_IMAGE], capture=True)
    deadline = time.time() + 90
    while time.time() < deadline:
        if _run(["docker", "exec", PG_NAME, "pg_isready", "-U", "transcria"], check=False, capture=True).returncode == 0:
            _log("pg", "PostgreSQL prêt")
            return
        time.sleep(3)
    _fail("pg", "PostgreSQL n'est pas devenu prêt (90s)")


def run_topology(topo: TopologySpec, distro_id: str, audio: Path, profile: str | None,
                 username: str, password: str, cuda: str | None, keep_up: bool,
                 llm_backend: str | None = None, stt_backend: str | None = None,
                 diarization_backend: str | None = None) -> None:
    import secrets

    spec_distro = get_distro(distro_id)
    names = [c.name for c in topo.containers]
    _log("setup", f"topologie={topo.name} distro={distro_id} ({spec_distro.base_image})")

    # Réseau propre + table rase.
    teardown(names)
    _run(["docker", "network", "create", NETWORK], check=False, capture=True)

    # Base applicative externe + secret de session jetables (DSN sans caractères à échapper).
    pg_password = secrets.token_hex(16)
    dsn = f"postgresql+psycopg://transcria:{pg_password}@{PG_NAME}:5432/transcria"
    session_secret = secrets.token_hex(24)

    try:
        if any(c.needs_db for c in topo.containers):
            start_postgres(pg_password)

        for c in topo.containers:
            _log("up", f"démarrage conteneur {c.name} (gpu={c.gpu})")
            env_set: dict[str, str] = {}
            if c.needs_db:
                env_set["TRANSCRIA_DATABASE_URL"] = dsn
                env_set["TRANSCRIA_SECRET"] = session_secret
            if c.runtime_role:
                env_set["TRANSCRIA_ROLE"] = c.runtime_role
            # Voie NON gated (whisper/sortformer) : autoriser le téléchargement HF au 1ᵉʳ run
            # (app.py fixe HF_HUB_OFFLINE=1 par setdefault → on l'écrase à 0 côté conteneur).
            if stt_backend or diarization_backend:
                env_set["HF_HUB_OFFLINE"] = "0"
            _run(docker_run_argv(c, spec_distro.base_image, _REPO,
                                 env_set=env_set, env_passthrough=SECRET_ENV_PASSTHROUGH), capture=True)

            # 1) Amorçage OS (apt/dnf + pièges ffmpeg) puis copie du dépôt en zone inscriptible.
            #    On EXCLUT venv/.git/backup/node_modules : le venv hôte n'est pas valide dans le
            #    conteneur (install.sh recrée le sien) et alourdirait inutilement la copie.
            for cmd in bootstrap_commands(distro_id):
                _docker_exec(c.name, cmd)
            _docker_exec(
                c.name,
                "mkdir -p /app && tar -C /src "
                "--exclude=./venv --exclude=./.git --exclude=./backup --exclude=./node_modules "
                "--exclude='*/__pycache__' -cf - . | tar -C /app -xf - && chmod -R u+w /app",
            )

            # 2) Config exemple → config.yaml (NODE_IP + DSN + mot de passe admin déterministe).
            _docker_exec(c.name, config_setup_command(
                c.config_example, NODE_NAME,
                dsn=dsn if c.needs_db else None,
                admin_password=password if c.needs_db else None,
                stt_backend=stt_backend, diarization_backend=diarization_backend,
            ))

            # 3) install.sh NATIF (torch CUDA + modèles ; --pg-existing pour les rôles à base ;
            #    --llm-backend force le moteur LLM en non-interactif — ollama pour la voie facile).
            _log("install", f"{c.name} : install.sh --profile {c.install_profile} (peut être long)")
            _docker_exec(c.name, install_command(c.install_profile, cuda, pg_existing=c.needs_db,
                                                 llm_backend=llm_backend))

            # 4) Lancement du service en arrière-plan.
            launch = " ".join(c.launch)
            _docker_exec(c.name, f"cd /app && nohup {launch} > /app/service.log 2>&1 &", detach=True)

        # 5) Santé + assertion GPU (conteneur GPU de la topologie).
        web = next(c for c in topo.containers if c.health_internal_port == 7870)
        node = next((c for c in topo.containers if c.name == topo.gpu_container), None)
        if node is not None:
            wait_health(node.published[node.health_internal_port], node.health_internal_port, node.name)
        wait_health(topo.web_host_port, 7870, web.name)
        assert_container_gpu(topo.gpu_container)

        # 6) E2E son via la frontale (réutilise le pilote éprouvé).
        from verify_split_topology import run_job  # type: ignore[import-not-found]

        web_url = f"http://localhost:{topo.web_host_port}"
        _log("e2e", f"job son E2E via {web_url} (profil={profile or 'défaut'})")
        run_job(web_url, audio, username, password, mode="quality",
                timeout_s=1800, poll_s=5, profile=profile)
        _log("e2e", "E2E OK — livrables produits (STT + diarisation + LLM + export)")
        print(f"\n✅ {topo.name} / {distro_id} : install.sh + GPU + LLM + E2E — SUCCÈS\n")
    finally:
        if keep_up:
            _log("teardown", f"--keep-up : conteneurs conservés ({', '.join(names)})")
        else:
            teardown(names)


def main() -> int:
    ap = argparse.ArgumentParser(description="Matrice install.sh × topologies + E2E GPU/LLM")
    ap.add_argument("--distro", required=True, choices=available_distros())
    ap.add_argument("--topology", required=True, choices=list(topologies()))
    ap.add_argument("--audio", type=Path, default=_REPO / "tests" / "test2.mp3")
    ap.add_argument("--profile", default=None, help="profil de traitement (ex. word_corrige)")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--password", default="matrix-admin-pw",
                    help="mot de passe admin injecté dans la config + utilisé pour l'E2E")
    ap.add_argument("--cuda", default=None, help="forcer l'index torch CUDA (ex. cu126)")
    ap.add_argument("--llm-backend", default=None, choices=("ollama", "llamacpp"),
                    help="forcer le backend LLM (ollama = voie facile sans compilation)")
    ap.add_argument("--stt-backend", default=None, help="forcer le backend STT (ex. whisper, non gated)")
    ap.add_argument("--diarization-backend", default=None, help="forcer la diarisation (ex. sortformer, non gated)")
    ap.add_argument("--keep-up", action="store_true", help="ne pas démonter (debug)")
    ap.add_argument("--no-preflight", action="store_true", help="sauter le preflight GPU hôte")
    args = ap.parse_args()

    if not args.audio.exists():
        _fail("setup", f"audio introuvable : {args.audio}")
    if not args.no_preflight:
        preflight_gpu()

    topo = topologies()[args.topology]
    run_topology(topo, args.distro, args.audio, args.profile,
                 args.username, args.password, args.cuda, args.keep_up,
                 llm_backend=args.llm_backend, stt_backend=args.stt_backend,
                 diarization_backend=args.diarization_backend)
    return 0


if __name__ == "__main__":
    sys.exit(main())

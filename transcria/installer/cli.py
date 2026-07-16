"""Point d'entrée de l'installateur Python : `python -m transcria.installer.cli`.

`install.sh` délègue les phases migrées à ce CLI et n'a plus qu'à vérifier le code
de sortie. Les nouvelles phases s'ajoutent ici en sous-commandes ; l'orchestration
métier vit dans les modules dédiés (testés), pas dans le shell.
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

# Le module doit rester importable par le python SYSTÈME sur distro vierge : la
# sous-commande python-env (celle qui CRÉE le venv et installe requirements.txt)
# tourne avant toute dépendance tierce. Seuls des imports stdlib-purs en tête ;
# les phases qui tirent PyYAML restent différées dans leur handler (§8.3c).
from transcria.installer.audiocpp_phase import (
    AUDIOCPP_PINNED_COMMIT,
    AudiocppPhaseError,
    AudiocppPlan,
    apply_audiocpp,
    resolve_runtimes_dir,
)
from transcria.installer.console import Console
from transcria.installer.i18n_phase import I18nError, I18nPlan, apply_i18n
from transcria.installer.moss_site_phase import MossSiteError, MossSitePlan, apply_moss_site
from transcria.installer.parakeetcpp_phase import (
    PARAKEETCPP_PINNED_COMMIT,
    ParakeetcppPhaseError,
    ParakeetcppPlan,
    apply_parakeetcpp,
)
from transcria.installer.postgres_phase import (
    PostgresBootstrapPlan,
    PostgresPhaseError,
    PostgresPlan,
    apply_postgres,
    apply_postgres_bootstrap,
)
from transcria.installer.python_env import PythonEnvError, PythonEnvPlan, apply_python_env
from transcria.installer.systemd_phase import SystemdPlan, apply_systemd


def _add_python_env_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "python-env",
        help="Provisionne le venv, PyTorch et les dépendances (SECTION 2-4 de install.sh).",
    )
    p.add_argument("--venv", required=True, help="Répertoire du venv (créé si absent)")
    p.add_argument("--requirements", required=True, help="Chemin de requirements.txt")
    p.add_argument("--skip-deps", action="store_true", help="Venv/dépendances déjà fournis : ne rien installer")
    p.add_argument("--no-torch", action="store_true", help="Ne pas installer PyTorch")
    p.add_argument("--cuda-version", default=None, help="Version CUDA détectée (ex. 12.4)")
    p.add_argument("--force-cuda", default=None, help="Forcer le tag wheel (cu121/cu124/cu126/cpu)")


def _add_i18n_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "i18n-compile",
        help="Compile les catalogues de traduction .po → .mo (interface multilingue).",
    )
    p.add_argument("--translations-dir", required=True, help="Dossier <locale>/LC_MESSAGES/messages.po")
    p.add_argument("--force", action="store_true", help="Recompiler même si le .mo est à jour")


def _cmd_i18n(args: argparse.Namespace) -> int:
    console = Console()
    plan = I18nPlan(translations_dir=Path(args.translations_dir), force=bool(args.force))
    try:
        apply_i18n(plan, console=console)
    except I18nError as exc:
        console.error(str(exc))
        return 1
    return 0


def _add_config_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "config",
        help="Génère config.yaml/.env + secrets + rôle runtime (cœur de SECTION 6).",
    )
    p.add_argument("--install-dir", required=True)
    p.add_argument("--config", required=True, help="Chemin de config.yaml")
    p.add_argument("--env-file", required=True)
    p.add_argument("--example-config", required=True, help="Chemin de config.example.yaml")
    p.add_argument("--env-template", required=True, help="Chemin de .env.example")
    p.add_argument("--profile", required=True)
    p.add_argument("--runtime-role", default="")
    p.add_argument("--profile-explicit", action="store_true")
    p.add_argument("--install-inference", action="store_true")
    p.add_argument("--force-config", action="store_true")


def _add_config_proxy_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "config-proxy",
        help="Persiste le proxy d'entreprise dans .env pour le service (bloc proxy de SECTION 6).",
    )
    p.add_argument("--env-file", required=True)
    p.add_argument("--proxy-https", required=True)
    p.add_argument("--proxy-http", required=True)
    p.add_argument("--proxy-no", required=True)
    p.add_argument("--service-user", default="")
    p.add_argument("--non-interactive", action="store_true")


def _add_opencode_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "opencode",
        help="Détecte/installe/configure opencode (SECTION 9).",
    )
    p.add_argument("--install-dir", required=True)
    p.add_argument("--config", required=True, help="Chemin de config.yaml")
    p.add_argument("--opencode-home", required=True)
    p.add_argument("--user-home", required=True)
    p.add_argument("--service-user", default="")
    p.add_argument("--profile", default="")
    p.add_argument("--needs-llm", action="store_true", help="Le profil requiert le LLM (sinon phase sautée)")
    p.add_argument("--non-interactive", action="store_true")
    p.add_argument("--current-path", default="")
    p.add_argument("--rc-file", action="append", default=[])


def _add_ollama_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "ollama",
        help="Installe/configure le backend LLM Ollama (scope all-in-one).",
    )
    p.add_argument("--config", required=True, help="Chemin de config.yaml")
    p.add_argument("--model", default="", help="Forcer le modèle Ollama (sinon résolu depuis le catalogue de profils selon le matériel)")
    p.add_argument("--gpu-count", type=int, default=1, help="Nb de GPU visibles (mono vs multi → spread)")
    p.add_argument("--per-card-vram-mb", type=int, default=0, help="VRAM de la carte la plus grande (Mio)")
    p.add_argument("--total-vram-mb", type=int, default=0, help="VRAM cumulée (Mio)")
    p.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    p.add_argument("--gpu-present", action="store_true", help="Un GPU NVIDIA est détecté (prérequis)")
    p.add_argument("--pin-version", default="", help="OLLAMA_VERSION épinglé (reproductibilité)")
    p.add_argument("--non-interactive", action="store_true")


def _add_postgres_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "postgres",
        help="Chemin post-connexion PostgreSQL : DSN, état, Alembic, migration SQLite (SECTION 6.5).",
    )
    p.add_argument("--host", required=True)
    p.add_argument("--port", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--user", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--install-dir", required=True)
    p.add_argument("--venv-python", required=True)
    p.add_argument("--env-file", required=True)
    p.add_argument("--sqlite-db", required=True)
    p.add_argument("--backup-dir", required=True)
    p.add_argument("--service-user", default="")
    p.add_argument("--local-pg", action="store_true", help="Base locale (autorise la reconstruction privilégiée)")
    p.add_argument("--non-interactive", action="store_true")
    p.add_argument("--pg-migrate", action="store_true", help="Migrer SQLite→PG sans prompt si la base PG est vide")
    p.add_argument("--defer", action="store_true",
                   help="Écrire le DSN SANS se connecter ni migrer (schéma déféré au runtime) — build d'image hermétique")
    p.add_argument("--admin-psql", default="", help="Préfixe psql privilégié pour le rebuild local (ex. 'sudo -u postgres psql')")


def _add_postgres_bootstrap_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "postgres-bootstrap",
        help="Provisionne une PostgreSQL locale : pg_hba + rôle + base (SECTION 6.5, privilégié).",
    )
    p.add_argument("--db", required=True)
    p.add_argument("--user", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--install-dir", required=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", default="5432")
    p.add_argument("--admin-psql", default="", help="Préfixe psql privilégié (ex. 'sudo -u postgres psql') ; vide = indisponible")
    p.add_argument("--admin-python", default="", help="Préfixe python privilégié -m (ex. 'sudo -u postgres env PYTHONPATH=… python -m')")
    p.add_argument("--have-systemctl", action="store_true")
    p.add_argument("--have-service", action="store_true")


def _add_systemd_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "systemd",
        help="Installe les unités systemd du profil (SECTION 11).",
    )
    p.add_argument("--profile", required=True)
    p.add_argument("--install-dir", required=True)
    p.add_argument("--service-user", required=True)
    p.add_argument("--service-home", required=True)
    p.add_argument("--venv-dir", required=True)
    p.add_argument("--no-service", action="store_true", help="N'installe ni le service legacy ni l'orchestration")
    p.add_argument("--install-inference", action="store_true")
    p.add_argument("--no-systemd", action="store_true", help="Aucune unité systemd (plan vide)")
    p.add_argument("--have-sudo", action="store_true")
    p.add_argument("--have-systemctl", action="store_true")


def _add_summary_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "summary",
        help="Affiche le résumé final de l'installation (SECTION 12, présentation).",
    )
    p.add_argument("--profile", required=True)
    p.add_argument("--install-dir", required=True)
    p.add_argument("--venv", required=True)
    p.add_argument("--config", required=True)
    p.add_argument("--inference-log-dir", required=True)
    p.add_argument("--final-log-file", required=True)
    p.add_argument("--db-backend", required=True)
    p.add_argument("--doctor-status", required=True)
    p.add_argument("--no-systemd", action="store_true")
    p.add_argument("--needs-local-models", action="store_true")
    p.add_argument("--needs-llm", action="store_true")
    p.add_argument("--cohere-ok", action="store_true")
    p.add_argument("--pyannote-ok", action="store_true")
    p.add_argument("--qwen-ok", action="store_true")
    p.add_argument("--opencode-bin", default="")


def _make_confirm(interactive: bool) -> "Callable[[str], bool]":
    def confirm(prompt: str) -> bool:
        if not interactive:
            return False
        try:
            answer = input(f"  {prompt} [o/N] : ")
        except EOFError:
            return False
        return answer.strip() in ("o", "O", "y", "Y")

    return confirm


def _cmd_opencode(args: argparse.Namespace) -> int:
    # Différé §8.3(c) : tire PyYAML — python-env (pré-venv, python système) ne doit pas le charger.
    from transcria.installer import opencode_phase

    console = Console()
    plan = opencode_phase.OpencodePlan(
        install_dir=Path(args.install_dir),
        config_path=Path(args.config),
        opencode_home=Path(args.opencode_home),
        user_home=Path(args.user_home),
        service_user=args.service_user,
        profile=args.profile,
        needs_llm=args.needs_llm,
        interactive=not args.non_interactive,
        current_path=args.current_path,
        rc_files=tuple(Path(p) for p in args.rc_file),
    )
    opencode_phase.apply_opencode(plan, console=console, confirm=_make_confirm(plan.interactive))
    return 0


def _cmd_ollama(args: argparse.Namespace) -> int:
    # Différé §8.3(c) : tire PyYAML — python-env (pré-venv, python système) ne doit pas le charger.
    from transcria.config import llm_profiles
    from transcria.installer import ollama_phase

    console = Console()
    # Modèle/contexte/spread résolus depuis le catalogue de données selon le MATÉRIEL
    # (mono vs multi-GPU) — plus aucun mapping hardcodé. --model force le choix si fourni.
    model, context, spread = args.model, 0, False
    gpu_indices: tuple[int, ...] = ()
    if not model:
        choice = llm_profiles.select_profile(
            llm_profiles.load_llm_profiles(_load_config_safe(args.config)), "ollama",
            gpu_count=args.gpu_count, per_card_vram_mb=args.per_card_vram_mb,
            total_vram_mb=args.total_vram_mb,
        )
        if choice is None:
            console.warn("Aucun palier Ollama ne tient dans la VRAM détectée — backend Ollama ignoré.")
            return 0
        model, context, spread = str(choice.model), choice.context, bool(choice.engine_env.get("OLLAMA_SCHED_SPREAD"))
        # gpu_indices : en mono-GPU [0], en multi-GPU spread [0, 1, ..., gpu_count-1].
        if choice.multi_gpu:
            gpu_indices = tuple(range(args.gpu_count))
        else:
            gpu_indices = (0,)
    plan = ollama_phase.OllamaPlan(
        config_path=Path(args.config),
        model=model,
        context=context,
        sched_spread=spread,
        ollama_url=args.ollama_url,
        gpu_present=args.gpu_present,
        interactive=not args.non_interactive,
        pin_version=args.pin_version,
        gpu_indices=gpu_indices,
    )
    ollama_phase.apply_ollama(plan, console=console, confirm=_make_confirm(plan.interactive))
    return 0


def _load_config_safe(path: str) -> dict:
    """Charge config.yaml pour l'override éventuel du catalogue de profils (best-effort)."""
    # Différé §8.3(c) : tire PyYAML — python-env (pré-venv, python système) ne doit pas le charger.
    from transcria.config import yaml_file

    try:
        return yaml_file.load_yaml_file(Path(path))
    except Exception:
        return {}


def _cmd_summary(args: argparse.Namespace) -> int:
    # Différé §8.3(c) : tire PyYAML — python-env (pré-venv, python système) ne doit pas le charger.
    from transcria.installer import summary_phase

    console = Console()
    plan = summary_phase.SummaryPlan(
        profile=args.profile,
        install_dir=Path(args.install_dir),
        venv=Path(args.venv),
        config_path=Path(args.config),
        inference_log_dir=args.inference_log_dir,
        final_log_file=args.final_log_file,
        db_backend=args.db_backend,
        doctor_status=args.doctor_status,
        needs_local_models=args.needs_local_models,
        needs_llm=args.needs_llm,
        cohere_ok=args.cohere_ok,
        pyannote_ok=args.pyannote_ok,
        qwen_ok=args.qwen_ok,
        opencode_bin=args.opencode_bin,
        systemd=not args.no_systemd,
    )
    summary_phase.apply_summary(plan, console=console)
    return 0


def _cmd_postgres_bootstrap(args: argparse.Namespace) -> int:
    import os
    import shlex

    console = Console()
    plan = PostgresBootstrapPlan(
        db=args.db,
        user=args.user,
        password=args.password,
        install_dir=Path(args.install_dir),
        host=args.host,
        port=args.port,
        is_root=os.geteuid() == 0,
        have_systemctl=args.have_systemctl,
        have_service=args.have_service,
        admin_psql_cmd=tuple(shlex.split(args.admin_psql)) if args.admin_psql else (),
        admin_python_cmd=tuple(shlex.split(args.admin_python)) if args.admin_python else (),
    )
    try:
        apply_postgres_bootstrap(plan, console=console)
    except PostgresPhaseError:
        return 1
    return 0


def _cmd_systemd(args: argparse.Namespace) -> int:
    import os

    console = Console()
    plan = SystemdPlan(
        profile=args.profile,
        install_dir=Path(args.install_dir),
        service_user=args.service_user,
        service_home=args.service_home,
        venv_dir=Path(args.venv_dir),
        install_service=not args.no_service,
        install_inference=args.install_inference,
        install_systemd=not args.no_systemd,
        euid=os.geteuid(),
        have_sudo=args.have_sudo,
        have_systemctl=args.have_systemctl,
    )
    apply_systemd(plan, console=console)
    return 0


def _cmd_postgres(args: argparse.Namespace) -> int:
    import os
    import shlex

    console = Console()
    plan = PostgresPlan(
        host=args.host,
        port=args.port,
        db=args.db,
        user=args.user,
        password=args.password,
        install_dir=Path(args.install_dir),
        venv_python=Path(args.venv_python),
        env_file=Path(args.env_file),
        sqlite_db=Path(args.sqlite_db),
        backup_dir=Path(args.backup_dir),
        service_user=args.service_user,
        local_pg=args.local_pg,
        non_interactive=args.non_interactive,
        pg_migrate=args.pg_migrate,
        pg_defer=args.defer,
        is_root=os.geteuid() == 0,
        admin_psql_cmd=tuple(shlex.split(args.admin_psql)) if args.admin_psql else (),
    )
    try:
        apply_postgres(plan, console=console)
    except PostgresPhaseError:
        return 1
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    # Différé §8.3(c) : tire PyYAML — python-env (pré-venv, python système) ne doit pas le charger.
    from transcria.installer import config_phase

    console = Console()
    plan = config_phase.ConfigPlan(
        install_dir=Path(args.install_dir),
        config_path=Path(args.config),
        env_file=Path(args.env_file),
        example_config=Path(args.example_config),
        env_template=Path(args.env_template),
        profile=args.profile,
        runtime_role=args.runtime_role,
        profile_explicit=args.profile_explicit,
        install_inference=args.install_inference,
        force_config=args.force_config,
    )
    config_phase.apply_config(plan, console=console)
    return 0


def _cmd_config_proxy(args: argparse.Namespace) -> int:
    import os

    # Différé §8.3(c) : tire PyYAML — python-env (pré-venv, python système) ne doit pas le charger.
    from transcria.installer import config_phase

    console = Console()
    plan = config_phase.ProxyPlan(
        env_file=Path(args.env_file),
        proxy_https=args.proxy_https,
        proxy_http=args.proxy_http,
        proxy_no=args.proxy_no,
        service_user=args.service_user,
        is_root=os.geteuid() == 0,
        interactive=not args.non_interactive,
    )
    config_phase.apply_proxy(plan, console=console)
    return 0


def _cmd_python_env(args: argparse.Namespace) -> int:
    console = Console()
    plan = PythonEnvPlan(
        venv_path=Path(args.venv),
        requirements_path=Path(args.requirements),
        skip_deps=args.skip_deps,
        install_torch=not args.no_torch,
        cuda_version=args.cuda_version,
        forced_cuda_tag=args.force_cuda,
    )
    try:
        apply_python_env(plan, console=console)
    except PythonEnvError as exc:
        console.error(str(exc))
        return 1
    return 0


def _add_recommend_llm_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("recommend-llm",
                       help="recommandation de moteur LLM pilotée par le matériel (catalogue de paliers)")
    p.add_argument("--gpu-count", type=int, required=True)
    p.add_argument("--per-card-vram-mb", type=int, required=True)
    p.add_argument("--total-vram-mb", type=int, required=True)
    p.add_argument("--config", default=None)


def _cmd_recommend_llm(args: argparse.Namespace) -> int:
    """Affiche la recommandation (lignes humaines) et termine par ``ENGINE=<moteur>``
    (ligne machine, consommée par install.sh). Ne choisit jamais À LA PLACE de
    l'utilisateur — C2.1 : recommander ET expliquer."""
    # Différé §8.3(c) : tire PyYAML — python-env (pré-venv, python système) ne doit pas le charger.
    from transcria.config import llm_profiles

    cfg = None
    if args.config:
        # Différé §8.3(c) : idem — le loader tire PyYAML.
        from transcria.config.loader import load_config

        cfg = load_config(args.config)
    profiles = llm_profiles.load_llm_profiles(cfg)
    rec = llm_profiles.recommend_engine(profiles,
                           gpu_count=args.gpu_count,
                           per_card_vram_mb=args.per_card_vram_mb,
                           total_vram_mb=args.total_vram_mb)
    label = "llama.cpp" if rec["engine"] == "llamacpp" else "Ollama"
    print(f"Moteur recommandé : {label}")
    if rec["reason"]:
        print(f"  → {rec['reason']}")
    print(f"ENGINE={rec['engine']}")
    return 0


def _add_moss_site_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "moss-site",
        help="Provisionne le site Transformers 5 isolé du backend STT `moss` (opt-in, ~800 Mo).",
    )
    p.add_argument("--dir", required=True, help="Répertoire cible du site (moss.moss_site dans config.yaml)")
    p.add_argument("--python", default=sys.executable, help="Python du venv (défaut : l'interpréteur courant)")
    p.add_argument("--force", action="store_true", help="Réinstaller même si le site semble complet")


def _cmd_moss_site(args: argparse.Namespace) -> int:
    import subprocess

    console = Console()
    plan = MossSitePlan(site_dir=Path(args.dir), python_bin=Path(args.python), force=bool(args.force))
    try:
        def _runner(cmd: list[str]) -> None:
            subprocess.run(cmd, check=True)

        apply_moss_site(plan, console=console, runner=_runner)
    except MossSiteError as exc:
        console.error(str(exc))
        return 1
    return 0


def _add_audiocpp_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "audiocpp",
        help="Provisionne le runtime STT servi audio.cpp (backend qwen3asr — clone épinglé + build CUDA, opt-in).",
    )
    p.add_argument("--runtimes-dir", default="./runtimes",
                   help="Racine des runtimes (défaut ./runtimes, surchargeable TRANSCRIA_RUNTIMES_DIR)")
    p.add_argument("--commit", default=None, help="Commit épinglé (défaut : celui qualifié sur le benchmark)")
    p.add_argument("--with-model", action="store_true",
                   help="Télécharge aussi le modèle recommandé Qwen3-ASR-1.7B-hf (~3,9 Go)")
    p.add_argument("--force", action="store_true", help="Reconstruire même si le runtime semble complet")


def _cmd_audiocpp(args: argparse.Namespace) -> int:
    import subprocess

    console = Console()

    def _runner(cmd: list[str], *, cwd: str | None = None) -> None:
        subprocess.run(cmd, check=True, cwd=cwd)

    plan = AudiocppPlan(
        runtimes_dir=resolve_runtimes_dir(args.runtimes_dir),
        commit=args.commit or AUDIOCPP_PINNED_COMMIT,
        with_model=bool(args.with_model),
        force=bool(args.force),
    )
    try:
        apply_audiocpp(plan, console=console, runner=_runner)
    except AudiocppPhaseError as exc:
        console.error(str(exc))
        return 1
    return 0


def _add_parakeetcpp_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "parakeetcpp",
        help="Provisionne le runtime STT servi parakeet.cpp (backend nemotron — clone épinglé + build CUDA, opt-in).",
    )
    p.add_argument("--runtimes-dir", default="./runtimes",
                   help="Racine des runtimes (défaut ./runtimes, surchargeable TRANSCRIA_RUNTIMES_DIR)")
    p.add_argument("--commit", default=None, help="Commit épinglé (défaut : celui qualifié sur le benchmark)")
    p.add_argument("--force", action="store_true", help="Reconstruire même si le runtime semble complet")


def _cmd_parakeetcpp(args: argparse.Namespace) -> int:
    import subprocess

    console = Console()

    def _runner(cmd: list[str], *, cwd: str | None = None) -> None:
        subprocess.run(cmd, check=True, cwd=cwd)

    plan = ParakeetcppPlan(
        runtimes_dir=resolve_runtimes_dir(args.runtimes_dir),
        commit=args.commit or PARAKEETCPP_PINNED_COMMIT,
        force=bool(args.force),
    )
    try:
        apply_parakeetcpp(plan, console=console, runner=_runner)
    except ParakeetcppPhaseError as exc:
        console.error(str(exc))
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="transcria-install", description="Installateur TranscrIA piloté en Python.")
    sub = parser.add_subparsers(dest="command", required=True)
    _add_python_env_parser(sub)
    _add_i18n_parser(sub)
    _add_config_parser(sub)
    _add_config_proxy_parser(sub)
    _add_opencode_parser(sub)
    _add_ollama_parser(sub)
    _add_postgres_parser(sub)
    _add_postgres_bootstrap_parser(sub)
    _add_systemd_parser(sub)
    _add_summary_parser(sub)
    _add_recommend_llm_parser(sub)
    _add_moss_site_parser(sub)
    _add_audiocpp_parser(sub)
    _add_parakeetcpp_parser(sub)
    args = parser.parse_args(argv)

    if args.command == "python-env":
        return _cmd_python_env(args)
    if args.command == "i18n-compile":
        return _cmd_i18n(args)
    if args.command == "config":
        return _cmd_config(args)
    if args.command == "config-proxy":
        return _cmd_config_proxy(args)
    if args.command == "opencode":
        return _cmd_opencode(args)
    if args.command == "ollama":
        return _cmd_ollama(args)
    if args.command == "postgres":
        return _cmd_postgres(args)
    if args.command == "postgres-bootstrap":
        return _cmd_postgres_bootstrap(args)
    if args.command == "systemd":
        return _cmd_systemd(args)
    if args.command == "summary":
        return _cmd_summary(args)
    if args.command == "recommend-llm":
        return _cmd_recommend_llm(args)
    if args.command == "moss-site":
        return _cmd_moss_site(args)
    if args.command == "audiocpp":
        return _cmd_audiocpp(args)
    if args.command == "parakeetcpp":
        return _cmd_parakeetcpp(args)
    parser.error(f"commande inconnue : {args.command}")  # pragma: no cover - argparse garde l'exhaustivité
    return 2


if __name__ == "__main__":
    sys.exit(main())

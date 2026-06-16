#!/usr/bin/env python3
"""Détecte et QUALIFIE le binaire llama-server (runtime de la LLM d'arbitrage).

Couche E/S autour de ``transcria.gpu.llama_runtime`` (logique pure, testée) :
  - recherche élargie des binaires (env, PATH, ~/llama.cpp, ~/ik_llama.cpp, /opt,
    /usr/local, envs conda) ;
  - collecte les faits : ``--version``, ``git describe`` dans l'arbre source si
    présent (source AUTORITAIRE de la version), ``ldd`` (libs résolues/manquantes) ;
  - rend un verdict et, si des libs manquent, propose un LLAMA_LD_LIBRARY_PATH.

Sortie : humaine (défaut), ``--format shell`` (KEY=VALUE à ``eval`` depuis
install.sh) ou ``--format json``. Codes de retour : 0 = utilisable (ok/warn),
2 = inutilisable (critical / introuvable), 1 = erreur d'exécution. Lecture seule :
ne lance JAMAIS de modèle, ne touche ni la config ni le GPU.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Permet l'exécution directe (`python scripts/detect_llama_server.py`) hors PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from transcria.gpu.llama_runtime import (  # noqa: E402
    MIN_BUILD,
    RuntimeReport,
    detect_cuda,
    evaluate_runtime,
    parse_git_describe,
    parse_ldd_output,
    parse_version_output,
)


def _run(cmd: list[str], *, merge_stderr: bool = False, timeout: int = 10) -> str | None:
    """Exécute une commande, renvoie stdout (str) ou None si indisponible/échec/timeout."""
    exe = shutil.which(cmd[0]) or (cmd[0] if os.path.isabs(cmd[0]) and os.access(cmd[0], os.X_OK) else None)
    if exe is None:
        return None
    try:
        result = subprocess.run(
            [exe, *cmd[1:]],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return result.stdout


def candidate_paths() -> list[str]:
    """Chemins candidats pour llama-server, par ordre de préférence, dédupliqués.

    Recherche ÉLARGIE (le trou des 3 chemins en dur) : env explicite, PATH, builds
    maison llama.cpp/ik_llama.cpp, /usr/local, /opt, et environnements conda.
    """
    out: list[str] = []
    env = os.environ.get("LLAMA_SERVER")
    if env:
        out.append(env)
    which = shutil.which("llama-server")
    if which:
        out.append(which)
    home = Path.home()
    fixed = [
        home / "llama.cpp/build/bin/llama-server",
        home / "ik_llama.cpp/build/bin/llama-server",
        Path("/usr/local/bin/llama-server"),
        Path("/usr/bin/llama-server"),
    ]
    out.extend(str(p) for p in fixed)
    cp = os.environ.get("CONDA_PREFIX")
    if cp:
        out.append(str(Path(cp) / "bin/llama-server"))
    for pattern in (
        str(home / ".conda/envs/*/bin/llama-server"),
        "/opt/*/bin/llama-server",
        "/opt/*/build/bin/llama-server",
    ):
        out.extend(sorted(glob.glob(pattern)))
    # Déduplique en conservant l'ordre.
    seen: set[str] = set()
    uniq: list[str] = []
    for p in out:
        rp = os.path.realpath(p)
        if rp in seen:
            continue
        seen.add(rp)
        uniq.append(p)
    return uniq


def _executables(paths: list[str]) -> list[str]:
    return [p for p in paths if os.path.isfile(p) and os.access(p, os.X_OK)]


def _find_source_repo(bin_path: str) -> str | None:
    """Remonte depuis le binaire pour trouver un arbre git source (jusqu'à 4 niveaux).

    Layout typique : ``<repo>/build/bin/llama-server`` → ``<repo>/.git``.
    """
    p = Path(bin_path).resolve()
    for parent in list(p.parents)[:4]:
        if (parent / ".git").exists():
            return str(parent)
    return None


def _suggest_ld_path(missing: list[str], resolved: dict[str, str]) -> str | None:
    """Cherche un répertoire contenant les .so manquants (envs conda voisins, etc.)."""
    if not missing:
        return None
    search_dirs: list[Path] = []
    cp = os.environ.get("CONDA_PREFIX")
    if cp:
        search_dirs.append(Path(cp) / "lib")
    # Répertoires des libs DÉJÀ résolues hors système : souvent là que vivent les autres.
    for path in resolved.values():
        d = Path(path).parent
        if d.parts[:2] != ("/", "usr") and d.parts[:2] != ("/", "lib"):
            search_dirs.append(d)
    search_dirs.extend(sorted(Path(p).parent for p in glob.glob(str(Path.home() / ".conda/envs/*/lib"))))
    seen: set[str] = set()
    for d in search_dirs:
        ds = str(d)
        if ds in seen:
            continue
        seen.add(ds)
        if all((d / name).exists() for name in missing):
            return ds
    return None


def collect_report(bin_path: str) -> tuple[RuntimeReport, str | None]:
    """Collecte les faits sur un binaire et rend (verdict, hint LD_LIBRARY_PATH)."""
    version_out = _run([bin_path, "--version"], merge_stderr=True, timeout=15)
    version_build, version_commit = parse_version_output(version_out)

    describe_build = describe_ahead = None
    describe_commit = None
    repo = _find_source_repo(bin_path)
    if repo is not None:
        describe_out = _run(["git", "-C", repo, "describe", "--tags"], timeout=10)
        describe_build, describe_ahead, describe_commit = parse_git_describe(describe_out)
    describe_ahead = describe_ahead or 0

    ldd_out = _run(["ldd", bin_path], timeout=15)
    resolved, missing = parse_ldd_output(ldd_out)
    has_cuda = detect_cuda(list(resolved) + missing)

    report = evaluate_runtime(
        path=bin_path,
        version_build=version_build,
        version_commit=version_commit,
        describe_build=describe_build,
        describe_ahead=describe_ahead,
        describe_commit=describe_commit,
        missing_libs=missing,
        has_cuda=has_cuda,
    )
    hint = _suggest_ld_path(missing, resolved)
    return report, hint


def _icon(level: str) -> str:
    return {"ok": "✓", "warn": "⚠", "critical": "✗"}.get(level, "•")


def _print_human(report: RuntimeReport, hint: str | None, others: list[str]) -> None:
    head = _icon(report.level)
    print(f"{head} llama-server : {report.path}")
    build = f"b{report.resolved_build}" if report.resolved_build is not None else "?"
    print(f"  version : {build} (source : {report.build_source}) — requis ≥ b{MIN_BUILD}")
    print(f"  CUDA    : {'oui' if report.has_cuda else 'non'}")
    for f in report.findings:
        print(f"  {_icon(f.level)} {f.message}")
    if hint:
        print(f"  → Réparation libs : export LLAMA_LD_LIBRARY_PATH={hint}")
    if others:
        print("  Autres binaires trouvés (ignorés) :", file=sys.stderr)
        for o in others:
            print(f"    - {o}", file=sys.stderr)


def _print_shell(report: RuntimeReport, hint: str | None) -> None:
    print("LLAMA_FOUND=1")
    print(f'LLAMA_SERVER="{report.path}"')
    print(f"LLAMA_OK={1 if report.usable else 0}")
    print(f"LLAMA_LEVEL={report.level}")
    print(f"LLAMA_BUILD={report.resolved_build if report.resolved_build is not None else ''}")
    print(f"LLAMA_BUILD_SOURCE={report.build_source}")
    print(f"LLAMA_HAS_CUDA={1 if report.has_cuda else 0}")
    print(f'LLAMA_LD_LIBRARY_PATH="{hint or ''}"')


def _report_to_dict(report: RuntimeReport, hint: str | None) -> dict:
    return {
        "path": report.path,
        "usable": report.usable,
        "level": report.level,
        "build": report.resolved_build,
        "build_source": report.build_source,
        "has_cuda": report.has_cuda,
        "missing_libs": report.missing_libs,
        "ld_library_path_hint": hint,
        "findings": [{"level": f.level, "message": f.message} for f in report.findings],
    }


def _emit_not_found(fmt: str) -> int:
    msg = (
        "Aucun binaire llama-server trouvé (env LLAMA_SERVER, PATH, ~/llama.cpp, "
        "~/ik_llama.cpp, /usr/local, /opt, envs conda). Compilez llama.cpp ≥ "
        f"b{MIN_BUILD} ou renseignez le chemin manuellement."
    )
    if fmt == "shell":
        print("LLAMA_FOUND=0")
        print('LLAMA_SERVER=""')
        print("LLAMA_OK=0")
    elif fmt == "json":
        print(json.dumps({"path": None, "usable": False, "reason": msg}, ensure_ascii=False, indent=2))
    else:
        print(f"✗ {msg}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Détecte et qualifie llama-server (runtime LLM d'arbitrage).")
    parser.add_argument("--bin", help="Chemin d'un binaire précis (sinon recherche automatique).")
    parser.add_argument("--format", choices=("human", "shell", "json"), default="human")
    args = parser.parse_args(argv)

    try:
        if args.bin:
            if not (os.path.isfile(args.bin) and os.access(args.bin, os.X_OK)):
                print(f"✗ Binaire inexécutable : {args.bin}", file=sys.stderr)
                return _emit_not_found(args.format)
            chosen = args.bin
            others: list[str] = []
        else:
            execs = _executables(candidate_paths())
            if not execs:
                return _emit_not_found(args.format)
            chosen, others = execs[0], execs[1:]

        report, hint = collect_report(chosen)

        if args.format == "shell":
            _print_shell(report, hint)
        elif args.format == "json":
            print(json.dumps(_report_to_dict(report, hint), ensure_ascii=False, indent=2))
        else:
            _print_human(report, hint, others)

        return 0 if report.usable else 2
    except Exception as exc:  # robustesse : jamais d'exception non gérée vers l'appelant
        print(f"✗ Erreur de détection : {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

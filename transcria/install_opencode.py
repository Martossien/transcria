from __future__ import annotations

import argparse
import os
import pwd
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import Callable

WhichFn = Callable[[str], str | None]
RunFn = Callable[..., subprocess.CompletedProcess[str]]

# Installateur officiel (https://opencode.ai/download). On délègue à lui plutôt que de
# télécharger un binaire « nu » : l'asset GitHub est désormais une archive versionnée par
# cible (arch x64/arm64, libc musl, variante AVX2 *baseline*), nommage qu'un lien direct
# ne suit plus (404). Le script gère archive/extraction/arch/musl/baseline/PATH.
OPENCODE_INSTALL_URL = "https://opencode.ai/install"


@dataclass(frozen=True)
class OpencodeDetection:
    binary: Path | None
    version: str


def opencode_version(binary: Path, run=subprocess.run) -> str:
    """Retourne la première ligne de `opencode --version`, ou un libellé de repli."""
    try:
        result = run([str(binary), "--version"], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return "version inconnue"
    output = result.stdout or result.stderr
    for line in output.splitlines():
        if line.strip():
            return line.strip()
    return "version inconnue"


def find_opencode_binary(
    *,
    opencode_home: Path,
    user_home: Path,
    configured_bin: str | None = None,
    which_fn: WhichFn = which,
) -> Path | None:
    """Cherche le binaire opencode dans l'ordre utilisé par l'installateur."""
    path_binary = which_fn("opencode")
    if path_binary:
        return Path(path_binary)

    candidates = [
        Path(opencode_home) / ".opencode" / "bin" / "opencode",
        Path(user_home) / ".opencode" / "bin" / "opencode",
    ]
    if configured_bin:
        candidates.append(Path(configured_bin))
    for candidate in candidates:
        if candidate.is_file() and candidate.stat().st_mode & 0o111:
            return candidate
    return None


def detect_opencode(
    *,
    opencode_home: Path,
    user_home: Path,
    configured_bin: str | None = None,
) -> OpencodeDetection:
    """Détecte opencode et sa version si le binaire existe."""
    binary = find_opencode_binary(opencode_home=opencode_home, user_home=user_home, configured_bin=configured_bin)
    return OpencodeDetection(binary=binary, version=opencode_version(binary) if binary else "")


def render_opencode_detection_shell(detection: OpencodeDetection) -> str:
    """Rend la détection opencode sous forme d'affectations shell filtrables."""
    values = {
        "OPENCODE_BIN": str(detection.binary or ""),
        "OPENCODE_VER": detection.version,
    }
    return "".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items())


def ensure_shell_path(opencode_dir: Path, rc_files: list[Path], *, current_path: str = "") -> Path | None:
    """Ajoute `opencode_dir` au premier fichier rc adapté et retourne le fichier modifié."""
    opencode_dir = Path(opencode_dir)
    opencode_dir_s = str(opencode_dir)
    path_entries = [entry for entry in current_path.split(":") if entry]
    if opencode_dir_s in path_entries:
        return None

    export_line = f'export PATH="{opencode_dir_s}:$PATH"'
    for rc in rc_files:
        rc = Path(rc)
        if not rc.is_file():
            continue
        content = rc.read_text(encoding="utf-8")
        if opencode_dir_s in content:
            return None
        suffix = "" if content.endswith("\n") or not content else "\n"
        rc.write_text(f"{content}{suffix}{export_line}\n", encoding="utf-8")
        return rc
    return None


def render_setup_log(*, event: str, value: str = "", profile: str = "") -> str:
    """Rend les messages d'installation opencode utilisés par install.sh."""
    if event == "found":
        return f"OK:opencode trouvé : {value}\n"
    if event == "missing":
        return "WARN:opencode non trouvé\n"
    if event == "download-start":
        return "INFO:Installation d'opencode via l'installateur officiel (opencode.ai/install)…\n"
    if event == "installed":
        return f"OK:opencode installé : {value}\n"
    if event == "path-updated":
        return f"OK:PATH mis à jour dans {value}\n"
    if event == "shell-reload":
        return f"INFO:Relancez votre shell ou : export PATH=\"{value}:$PATH\"\n"
    if event == "download-failed":
        return "ERROR:Téléchargement opencode échoué — vérifiez la connectivité\n"
    if event == "manual-title":
        return "INFO:Installation manuelle d'opencode (voir https://opencode.ai/download) :\n"
    if event == "manual-curl":
        return "INFO:  curl -fsSL https://opencode.ai/install | bash\n"
    if event == "manual-alt":
        return "INFO:  ou : npm i -g opencode-ai  |  bun add -g opencode-ai  |  brew install anomalyco/tap/opencode\n"
    if event == "ignored":
        return "INFO:opencode ignoré — résumé/correction LLM désactivé\n"
    if event == "install-later":
        return "INFO:Pour installer plus tard : https://opencode.ai\n"
    if event == "configure-start":
        return "INFO:Configuration du provider opencode local…\n"
    if event == "provider-ok":
        return "OK:opencode provider local configuré\n"
    if event == "provider-incomplete":
        return f"WARN:Configuration opencode incomplète — relancez : {value}\n"
    if event == "profile-skipped":
        return f"INFO:Profil {profile} : opencode non requis\n"
    raise ValueError(f"événement opencode inconnu : {event}")


def render_install_prompt(*, opencode_home: Path) -> str:
    """Rend la question d'installation interactive opencode."""
    return f"Installer opencode dans {opencode_home}/.opencode/bin/ ?"


def _best_effort_chown_tree(path: Path, service_user: str) -> None:
    if not service_user:
        return
    try:
        user = pwd.getpwnam(service_user)
    except KeyError:
        return
    for root, dirs, files in os.walk(path):
        for name in [".", *dirs, *files]:
            target = Path(root) if name == "." else Path(root) / name
            try:
                os.chown(target, user.pw_uid, user.pw_gid)
            except OSError:
                pass


def install_opencode_binary(
    *,
    opencode_home: Path,
    install_url: str = OPENCODE_INSTALL_URL,
    service_user: str = "",
    run: RunFn = subprocess.run,
    env: dict[str, str] | None = None,
) -> bool:
    """Installe opencode via l'installateur officiel et ajuste le propriétaire si possible.

    On délègue au script officiel (`curl -fsSL {install_url} | bash`) au lieu de récupérer un
    binaire « nu » : lui seul choisit le bon asset (archive .tar.gz/.zip), l'architecture
    (x64/arm64), la libc musl et surtout la variante AVX2 *baseline* — sans elle, le binaire
    standard plante en « illegal instruction » sur un CPU/VM sans AVX2. Le script installe dans
    `$HOME/.opencode/bin/opencode` ; on force `HOME=opencode_home` pour cibler le bon répertoire
    (root ou utilisateur de service), puis chown best-effort vers l'utilisateur de service.
    """
    opencode_home = Path(opencode_home)
    run_env = dict(os.environ if env is None else env)
    run_env["HOME"] = str(opencode_home)
    result = run(["bash", "-c", f"curl -fsSL {shlex.quote(install_url)} | bash"], check=False, env=run_env)
    if getattr(result, "returncode", 1) != 0:
        return False

    binary = opencode_home / ".opencode" / "bin" / "opencode"
    if not binary.is_file():
        # Le script a « réussi » mais le binaire attendu n'est pas là (réseau partiel, cible
        # inattendue) : on échoue explicitement pour basculer sur les instructions manuelles.
        return False
    if service_user:
        _best_effort_chown_tree(opencode_home / ".opencode", service_user)
    return True


def classify_opencode_install(binary: Path) -> str:
    """Type d'install d'un binaire opencode : ``'npm'`` | ``'official'`` | ``'brew'`` | ``'unknown'``.

    Résout d'abord les liens symboliques (l'install npm expose typiquement un symlink PATH
    ``/usr/local/bin/opencode`` → ``…/node_modules/opencode-ai/bin/opencode.exe``), puis reconnaît
    la source à l'emplacement RÉEL. Sert à choisir le bon updater (npm ≠ self-update officiel)."""
    try:
        real = Path(os.path.realpath(binary)).as_posix()
    except OSError:
        real = Path(binary).as_posix()
    if "node_modules/opencode-ai" in real:
        return "npm"
    if "/.opencode/bin/" in real:
        return "official"
    if "/Cellar/opencode" in real or "/homebrew/" in real:
        return "brew"
    return "unknown"


def opencode_upgrade_command(kind: str, binary: Path) -> list[str] | None:
    """Commande de mise à jour pour un type d'install (``None`` si inconnu)."""
    if kind == "official":
        return [str(binary), "upgrade"]  # binaire officiel auto-actualisable en place
    if kind == "npm":
        return ["npm", "install", "-g", "opencode-ai@latest"]
    if kind == "brew":
        return ["brew", "upgrade", "opencode"]
    return None


@dataclass(frozen=True)
class OpencodeUpgradeResult:
    kind: str
    ok: bool
    version_before: str
    version_after: str
    message: str


def upgrade_opencode(
    *,
    binary: Path,
    kind: str | None = None,
    run: RunFn = subprocess.run,
    env: dict[str, str] | None = None,
) -> OpencodeUpgradeResult:
    """Met à jour opencode selon son type d'install détecté (``run`` injectable pour les tests).

    ``official`` → self-update en place (``opencode upgrade``) ; ``npm`` → ``npm i -g
    opencode-ai@latest`` ; ``brew`` → ``brew upgrade opencode``. Pour l'officiel on force ``HOME``
    sur le parent de ``.opencode`` afin de cibler CET install (piège root ≠ utilisateur de service :
    ``/root/.opencode`` vs ``~/.opencode``). Compare la version avant/après pour un rapport net."""
    kind = kind or classify_opencode_install(binary)
    command = opencode_upgrade_command(kind, binary)
    before = opencode_version(binary, run=run)
    if command is None:
        return OpencodeUpgradeResult(
            kind="unknown", ok=False, version_before=before, version_after=before,
            message="type d'install opencode inconnu — mise à jour manuelle requise "
                    "(npm i -g opencode-ai@latest | opencode upgrade | brew upgrade opencode)",
        )
    run_env = dict(os.environ if env is None else env)
    if kind == "official":
        # binary = <home>/.opencode/bin/opencode → HOME = <home>
        run_env["HOME"] = str(Path(binary).parent.parent.parent)
    try:
        result = run(command, capture_output=True, text=True, timeout=600, check=False, env=run_env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return OpencodeUpgradeResult(kind, False, before, before, f"échec du lancement : {exc}")
    after = opencode_version(binary, run=run)
    if getattr(result, "returncode", 1) != 0:
        err = (getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").strip()[:300]
        return OpencodeUpgradeResult(kind, False, before, after,
                                     f"échec (code {getattr(result, 'returncode', '?')}) : {err}")
    message = f"déjà à jour ({after})" if after == before else f"mis à jour : {before} → {after}"
    return OpencodeUpgradeResult(kind, True, before, after, message)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Helpers d'installation opencode TranscrIA.")
    parser.add_argument("--version", action="store_true", help="affiche la version opencode")
    parser.add_argument("--find", action="store_true", help="cherche le binaire opencode")
    parser.add_argument("--detect", action="store_true", help="cherche opencode et rend OPENCODE_BIN/OPENCODE_VER")
    parser.add_argument("--ensure-path", action="store_true", help="ajoute le dossier opencode au shell rc si nécessaire")
    parser.add_argument("--install-binary", action="store_true", help="télécharge et prépare le binaire opencode")
    parser.add_argument("--setup-log", action="store_true", help="rend un message d'installation opencode")
    parser.add_argument("--install-prompt", action="store_true", help="rend la question d'installation opencode")
    parser.add_argument("--bin", default=None, help="chemin du binaire opencode")
    parser.add_argument("--opencode-dir", default=None)
    parser.add_argument("--opencode-home", default=None)
    parser.add_argument("--user-home", default=None)
    parser.add_argument("--configured-bin", default=None)
    parser.add_argument("--current-path", default="")
    parser.add_argument("--rc-file", action="append", default=[])
    parser.add_argument("--event", default="")
    parser.add_argument("--value", default="")
    parser.add_argument("--profile", default="")
    parser.add_argument("--install-url", default=OPENCODE_INSTALL_URL)
    parser.add_argument("--service-user", default="")
    args = parser.parse_args(argv)

    if args.version:
        if not args.bin:
            print("--bin requis avec --version", file=sys.stderr)
            return 2
        print(opencode_version(Path(args.bin)))
        return 0
    if args.find:
        if not args.opencode_home or not args.user_home:
            print("--opencode-home et --user-home requis avec --find", file=sys.stderr)
            return 2
        binary = find_opencode_binary(
            opencode_home=Path(args.opencode_home),
            user_home=Path(args.user_home),
            configured_bin=args.configured_bin,
        )
        if binary is None:
            return 1
        print(binary)
        return 0
    if args.detect:
        if not args.opencode_home or not args.user_home:
            print("--opencode-home et --user-home requis avec --detect", file=sys.stderr)
            return 2
        print(
            render_opencode_detection_shell(
                detect_opencode(
                    opencode_home=Path(args.opencode_home),
                    user_home=Path(args.user_home),
                    configured_bin=args.configured_bin,
                )
            ),
            end="",
        )
        return 0
    if args.ensure_path:
        if not args.opencode_dir:
            print("--opencode-dir requis avec --ensure-path", file=sys.stderr)
            return 2
        rc_files = [Path(path) for path in args.rc_file]
        updated = ensure_shell_path(Path(args.opencode_dir), rc_files, current_path=args.current_path)
        if updated is None:
            return 1
        print(updated)
        return 0
    if args.install_binary:
        if not args.opencode_home:
            print("--opencode-home requis avec --install-binary", file=sys.stderr)
            return 2
        return (
            0
            if install_opencode_binary(
                opencode_home=Path(args.opencode_home),
                install_url=args.install_url,
                service_user=args.service_user,
            )
            else 1
        )
    if args.setup_log:
        if not args.event:
            print("--event requis avec --setup-log", file=sys.stderr)
            return 2
        print(render_setup_log(event=args.event, value=args.value, profile=args.profile), end="")
        return 0
    if args.install_prompt:
        if not args.opencode_home:
            print("--opencode-home requis avec --install-prompt", file=sys.stderr)
            return 2
        print(render_install_prompt(opencode_home=Path(args.opencode_home)), end="")
        return 0
    print("commande opencode inconnue", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

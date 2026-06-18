from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import Callable

WhichFn = Callable[[str], str | None]


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
        return "INFO:Téléchargement opencode (linux-x64)...\n"
    if event == "installed":
        return f"OK:opencode installé : {value}\n"
    if event == "path-updated":
        return f"OK:PATH mis à jour dans {value}\n"
    if event == "shell-reload":
        return f"INFO:Relancez votre shell ou : export PATH=\"{value}:$PATH\"\n"
    if event == "download-failed":
        return "ERROR:Téléchargement opencode échoué — vérifiez la connectivité\n"
    if event == "manual-title":
        return "INFO:Installation manuelle :\n"
    if event == "manual-mkdir":
        return "INFO:  mkdir -p ~/.opencode/bin\n"
    if event == "manual-curl":
        return "INFO:  curl -fsSL -o ~/.opencode/bin/opencode https://github.com/anomalyco/opencode/releases/latest/download/opencode-linux-x64\n"
    if event == "manual-chmod":
        return "INFO:  chmod +x ~/.opencode/bin/opencode\n"
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Helpers d'installation opencode TranscrIA.")
    parser.add_argument("--version", action="store_true", help="affiche la version opencode")
    parser.add_argument("--find", action="store_true", help="cherche le binaire opencode")
    parser.add_argument("--detect", action="store_true", help="cherche opencode et rend OPENCODE_BIN/OPENCODE_VER")
    parser.add_argument("--ensure-path", action="store_true", help="ajoute le dossier opencode au shell rc si nécessaire")
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

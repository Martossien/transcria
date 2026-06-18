from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from shutil import which
from typing import Callable

WhichFn = Callable[[str], str | None]


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Helpers d'installation opencode TranscrIA.")
    parser.add_argument("--version", action="store_true", help="affiche la version opencode")
    parser.add_argument("--find", action="store_true", help="cherche le binaire opencode")
    parser.add_argument("--bin", default=None, help="chemin du binaire opencode")
    parser.add_argument("--opencode-home", default=None)
    parser.add_argument("--user-home", default=None)
    parser.add_argument("--configured-bin", default=None)
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
    print("commande opencode inconnue", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

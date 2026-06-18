from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Helpers d'installation opencode TranscrIA.")
    parser.add_argument("--version", action="store_true", help="affiche la version opencode")
    parser.add_argument("--bin", required=True, help="chemin du binaire opencode")
    args = parser.parse_args(argv)

    if args.version:
        print(opencode_version(Path(args.bin)))
        return 0
    print("commande opencode inconnue", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

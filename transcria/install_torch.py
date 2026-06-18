from __future__ import annotations

import argparse
import importlib
import re
import sys
from types import ModuleType
from typing import Any

_CUDA_VERSION_RE = re.compile(r"^\s*(?P<major>\d+)(?:\.(?P<minor>\d+))?")


def select_torch_cuda_tag(cuda_version: str | None = None, *, forced_tag: str | None = None) -> tuple[str, str | None]:
    """Sélectionne le tag wheel PyTorch (`cpu`, `cu121`, `cu124`, `cu126`).

    Returns:
        `(tag, warning)` ; `warning` vaut `None` quand aucune dégradation n'est nécessaire.
    """
    if forced_tag:
        return forced_tag, None
    if not cuda_version:
        return "cpu", "CUDA non détecté — PyTorch CPU uniquement"

    match = _CUDA_VERSION_RE.match(cuda_version)
    if not match:
        return "cu121", f"CUDA {cuda_version} illisible — cu121 utilisé par défaut"

    major = int(match.group("major"))
    minor = int(match.group("minor") or 0)
    if major > 12 or (major == 12 and minor >= 6):
        return "cu126", None
    if major == 12 and minor >= 4:
        return "cu124", None
    if major == 12 and minor >= 1:
        return "cu121", None
    return "cu121", f"CUDA {cuda_version} — cu121 utilisé par défaut"


def installed_torch_cuda_version(import_module: Any = importlib.import_module) -> str:
    """Retourne la version CUDA de torch installé, `cpu`, ou une chaîne vide si absent."""
    try:
        torch: ModuleType = import_module("torch")
    except ImportError:
        return ""
    version = getattr(torch, "version", None)
    cuda = getattr(version, "cuda", None)
    return str(cuda or "cpu")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sélectionne le tag PyTorch adapté à la version CUDA détectée.")
    parser.add_argument("--cuda-version", default=None)
    parser.add_argument("--force-cuda", default=None)
    parser.add_argument("--installed-cuda", action="store_true", help="affiche la version CUDA du torch installé, ou vide si absent")
    parser.add_argument("--format", choices=("tag", "shell"), default="tag")
    args = parser.parse_args(argv)

    if args.installed_cuda:
        installed = installed_torch_cuda_version()
        if installed:
            print(installed)
        return 0

    tag, warning = select_torch_cuda_tag(args.cuda_version, forced_tag=args.force_cuda)
    if args.format == "shell":
        print(f"CUDA_TAG={tag}")
        if warning:
            print(f"CUDA_WARNING={warning!r}")
        else:
            print("CUDA_WARNING=''")
    else:
        print(tag)
        if warning:
            print(warning, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

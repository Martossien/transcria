#!/usr/bin/env python3
"""Garde-fou de BUILD : l'environnement natif du SDK Zoom est-il complet ?

Pourquoi au build et pas à l'exécution : une bibliothèque manquante ne produit pas d'erreur
lisible côté SDK — le processus **plante par segfault**, au milieu d'une réunion, sans rien
dire d'utile. Échouer ici rend le diagnostic immédiat, avec le nom de la bibliothèque.

Deux contrôles, du plus concluant au plus large :

1. **Import réel du module.** C'est la seule preuve que le chargeur dynamique résout
   effectivement toute la chaîne, `RPATH` compris. Un `ldd` qui passe mais un import qui
   échoue reste possible ; l'inverse aussi.
2. **Balayage `ldd`.** Il attrape ce que l'import ne voit pas : les bibliothèques chargées
   PARESSEUSEMENT (greffons de plateforme Qt, pilotes GL) n'apparaissent qu'au premier usage,
   c'est-à-dire trop tard — en réunion.

⚠ Subtilité qui a produit 21 faux positifs à la première version : les bibliothèques
embarquées vivent dans `zoom_meeting_sdk.libs/` sous des noms hachés (`libXau-154567c4.so.6`)
et ne déclarent PAS de `RPATH` vers leur propre répertoire — seuls les bindings le font, via
`$ORIGIN/../zoom_meeting_sdk.libs`. Inspecter une bibliothèque embarquée sans reproduire ce
chemin la fait déclarer « introuvable » alors qu'elle est là. On fixe donc `LD_LIBRARY_PATH`
sur les répertoires du SDK, ce qui reproduit les conditions réelles de chargement.
"""
from __future__ import annotations

import os
import subprocess
import sys
import sysconfig
from pathlib import Path

MODULE = "zoom_meeting_sdk"


def site_roots() -> set[Path]:
    """Répertoires d'installation RÉELS des paquets — ils diffèrent entre Debian, Ubuntu et
    une image `python:*`, d'où la lecture via `sysconfig` plutôt que des chemins devinés."""
    return {
        Path(path)
        for key in ("purelib", "platlib")
        if (path := sysconfig.get_paths().get(key))
    }


def sdk_libraries() -> tuple[list[Path], set[Path]]:
    """(bibliothèques du SDK, répertoires qui les contiennent)."""
    libraries: list[Path] = []
    for root in site_roots():
        for pattern in (f"{MODULE}/*.so*", f"{MODULE}.libs/*.so*"):
            libraries.extend(sorted(root.glob(pattern)))
    return libraries, {library.parent for library in libraries}


def unresolved(library: Path, search_path: str) -> list[str]:
    """Noms des dépendances non résolues de `library`, dans les conditions réelles de
    chargement (`LD_LIBRARY_PATH` = répertoires du SDK)."""
    env = dict(os.environ, LD_LIBRARY_PATH=search_path)
    result = subprocess.run(["ldd", str(library)], capture_output=True, text=True,
                            check=False, env=env)
    return [
        line.strip().split(" =>")[0].strip()
        for line in result.stdout.splitlines()
        if "not found" in line
    ]


def check_import() -> str:
    """Message d'erreur si l'import échoue, chaîne vide si tout va bien."""
    result = subprocess.run([sys.executable, "-c", f"import {MODULE}"],
                            capture_output=True, text=True, check=False)
    if result.returncode == 0:
        return ""
    # Le SDK bavarde sur stdout/stderr même quand tout va bien : on ne garde que la fin.
    detail = (result.stderr or result.stdout).strip().splitlines()
    return "\n".join(detail[-5:]) if detail else f"code de sortie {result.returncode}"


def main() -> int:
    libraries, directories = sdk_libraries()
    if not libraries:
        print(f"ÉCHEC : paquet `{MODULE}` introuvable — installation ratée en silence.",
              file=sys.stderr)
        return 1

    failure = check_import()
    if failure:
        print(f"ÉCHEC : `import {MODULE}` ne passe pas :\n{failure}", file=sys.stderr)
        return 1

    search_path = os.pathsep.join(str(directory) for directory in sorted(directories))
    missing = {
        name
        for library in libraries
        for name in unresolved(library, search_path)
    }
    if missing:
        print(f"ÉCHEC : {len(missing)} dépendance(s) native(s) manquante(s) pour le SDK Zoom :",
              file=sys.stderr)
        for name in sorted(missing):
            print(f"  - {name}", file=sys.stderr)
        print("\nAjouter le paquet système correspondant dans Dockerfile.zoom-sdk "
              "(`apt-file search <nom>` pour le retrouver).", file=sys.stderr)
        return 1

    print(f"SDK Zoom : import OK, {len(libraries)} bibliothèque(s) vérifiée(s), "
          f"aucune dépendance manquante.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Garde CI de l'internationalisation (interface, axe A).

Vérifie, pour chaque catalogue ``transcria/web/translations/<locale>/LC_MESSAGES/messages.po`` :

  1. **À jour** : toutes les chaînes marquées dans le code/les templates (extraction ``pybabel``)
     sont présentes dans le catalogue. Sinon → il manque un ``pybabel update`` (échec).
  2. **Complet** : aucun ``msgstr`` vide (traduction manquante = build rouge). Le catalogue de la
     langue source (``i18n.default_locale``) est rempli en identité par l'outillage, donc jamais
     vide non plus.
  3. **Compile** : produit les ``.mo`` (gitignorés) pour que l'app et les tests disposent des
     traductions.

Sortie non nulle au premier problème, avec un message actionnable. Voir docs/I18N_MULTILANGUE.md.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRANSLATIONS = ROOT / "transcria" / "web" / "translations"
BABEL_CFG = ROOT / "babel.cfg"


def _extract_pot(dest: Path) -> None:
    # Via `-m babel.messages.frontend` : indépendant du PATH (le binaire `pybabel` peut ne pas
    # y être quand on lance le python du venv directement).
    subprocess.run(
        [sys.executable, "-m", "babel.messages.frontend", "extract", "-F", str(BABEL_CFG),
         "-o", str(dest), "--project=TranscrIA", "--no-wrap", str(ROOT)],
        check=True, cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _keys(catalog) -> set[tuple[str | None, object]]:
    return {(m.context, m.id) for m in catalog if m.id}


def main() -> int:
    from babel.messages.mofile import write_mo
    from babel.messages.pofile import read_po

    if not TRANSLATIONS.exists():
        print(f"[i18n] dossier de traductions absent : {TRANSLATIONS}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        pot_path = Path(tmp) / "messages.pot"
        _extract_pot(pot_path)
        with pot_path.open("rb") as fh:
            pot_keys = _keys(read_po(fh))

    po_files = sorted(TRANSLATIONS.glob("*/LC_MESSAGES/messages.po"))
    if not po_files:
        print(f"[i18n] aucun catalogue .po sous {TRANSLATIONS}", file=sys.stderr)
        return 1

    failed = False
    for po_path in po_files:
        locale = po_path.parent.parent.name
        with po_path.open("rb") as fh:
            catalog = read_po(fh, locale=locale)
        po_keys = _keys(catalog)

        missing = pot_keys - po_keys
        if missing:
            failed = True
            print(f"[i18n] catalogue '{locale}' PAS À JOUR : {len(missing)} chaîne(s) marquée(s) "
                  f"absente(s). Lancez : pybabel update -i messages.pot -d "
                  f"transcria/web/translations", file=sys.stderr)
            for ctx, mid in list(missing)[:5]:
                print(f"        - ({ctx or '-'}) {mid!r}"[:100], file=sys.stderr)

        empty = [m for m in catalog if m.id and not _has_translation(m)]
        if empty:
            failed = True
            print(f"[i18n] catalogue '{locale}' INCOMPLET : {len(empty)} traduction(s) vide(s).",
                  file=sys.stderr)
            for m in empty[:5]:
                key = m.id[0] if isinstance(m.id, (list, tuple)) else m.id
                print(f"        - {key!r}"[:100], file=sys.stderr)

        # Compile (produit le .mo même si le catalogue est incomplet — les tests suivants
        # tournent, mais le job CI échoue à cause de `failed`).
        mo_path = po_path.with_suffix(".mo")
        with mo_path.open("wb") as fh:
            write_mo(fh, catalog)

    if failed:
        return 1
    print(f"[i18n] OK : {len(po_files)} catalogue(s) à jour, complet(s) et compilé(s).")
    return 0


def _has_translation(message) -> bool:
    if isinstance(message.id, (list, tuple)):  # pluriel : toutes les formes remplies
        return bool(message.string) and all(message.string)
    return bool(message.string)


if __name__ == "__main__":
    sys.exit(main())

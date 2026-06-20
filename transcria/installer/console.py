"""Rendu console de l'installateur — fidèle au style de `install.sh`.

L'installateur Python possède désormais sa propre sortie (au lieu d'émettre des
lignes préfixées `OK:`/`INFO:` redispatché par le shell). Les couleurs reprennent
celles de `install.sh` (`[INFO]` bleu, `[OK]` vert, `[WARN]` jaune, `[ERROR]`
rouge, sections en gras) pour une expérience continue. Les codes ANSI sont omis
hors terminal ou si `NO_COLOR` est défini, ce qui rend la sortie capturée (tests,
journaux, `tee`) lisible.
"""
from __future__ import annotations

import os
import sys
from typing import TextIO

_RESET = "\033[0m"
_COLORS = {
    "INFO": "\033[0;34m",
    "OK": "\033[0;32m",
    "WARN": "\033[1;33m",
    "ERROR": "\033[0;31m",
    "SECTION": "\033[1m\033[0;34m",
}


class Console:
    """Émet les messages d'installation avec le même habillage que `install.sh`."""

    def __init__(self, stream: TextIO | None = None, *, color: bool | None = None) -> None:
        self._stream = stream or sys.stdout
        if color is None:
            color = self._stream.isatty() and os.environ.get("NO_COLOR") is None
        self._color = color

    def _emit(self, level: str, label: str, message: str) -> None:
        if self._color:
            tag = f"{_COLORS[level]}{label}{_RESET}"
        else:
            tag = label
        print(f"{tag} {message}", file=self._stream, flush=True)

    def info(self, message: str) -> None:
        self._emit("INFO", "[INFO] ", message)

    def ok(self, message: str) -> None:
        self._emit("OK", "[OK]   ", message)

    def warn(self, message: str) -> None:
        self._emit("WARN", "[WARN] ", message)

    def error(self, message: str) -> None:
        self._emit("ERROR", "[ERROR]", message)

    def section(self, title: str) -> None:
        line = f"═══ {title} ═══"
        if self._color:
            line = f"{_COLORS['SECTION']}{line}{_RESET}"
        print(f"\n{line}", file=self._stream, flush=True)

    def write(self, text: str = "") -> None:
        """Émet du texte verbatim (sans tag ni couleur) — pour les blocs de résumé."""
        print(text, file=self._stream, flush=True)

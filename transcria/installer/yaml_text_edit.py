"""Écriture CHIRURGICALE de scalaires dans un YAML existant (config.yaml) — diff minimal.

Pourquoi pas un round-trip ruamel : ``config.yaml`` mélange plusieurs styles d'indentation de
listes (``offset`` 0 et 2) et des ``null`` explicites ; un re-dump les normalise (≈ 100 lignes
modifiées sur ``config.example.yaml``, ``null`` → vide). Ici on ne touche QUE les lignes des
clés écrites (ou on insère les clés absentes sous leur section), puis on VÉRIFIE par
relecture PyYAML que le document obtenu est exactement l'original + ces valeurs. Tout écart
(style flow ``{a: 1}``, ancre, clé qui serait une section…) lève ``YamlTextEditError`` sans
rien écrire.

L'écriture est atomique (``mkstemp`` dans le même dossier) et CONSERVE le mode et le
propriétaire de l'original : config.yaml porte des secrets (``first_admin_password``,
``smtp_password``) — un 0600 ne doit jamais redevenir 0644, ni passer à root quand root installe.
"""
from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml

from transcria.config.yaml_file import set_yaml_value

_PLAIN_SAFE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")
_YAML_KEYWORDS = {"true", "false", "yes", "no", "on", "off", "null", "y", "n", "~"}
_VALUE_RE = re.compile(r"""^(?P<head>\s*[^\s#:][^:]*?\s*:)(?P<value>\s*(?:"(?:[^"\\]|\\.)*"|'[^']*'|[^#]*?))(?P<tail>\s+#.*)?$""")


class YamlTextEditError(ValueError):
    """Le fichier ne se prête pas à une écriture chirurgicale sûre."""


def render_scalar(value: Any) -> str:
    """Représentation YAML d'un scalaire (bool, int, str) — guillemets doubles si nécessaire."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value)
    if _PLAIN_SAFE.match(text) and text.lower() not in _YAML_KEYWORDS and not text[0].isdigit():
        return text
    return json.dumps(text, ensure_ascii=False)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_content(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _key_of(line: str) -> str | None:
    m = re.match(r"^\s*([^\s#:'\"][^:#]*?)\s*:(\s|$)", line)
    return m.group(1) if m else None


def _block_end(lines: list[str], start: int, parent_indent: int) -> int:
    """Fin (exclue) du bloc des enfants qui commence à ``start`` (indentation > parent)."""
    end = start
    for i in range(start, len(lines)):
        if _is_content(lines[i]):
            if _indent(lines[i]) <= parent_indent:
                break
            end = i + 1
    return end


def _set_one(lines: list[str], dotted: str, value: Any) -> None:
    rendered = render_scalar(value)
    parts = dotted.split(".")
    lo, hi, parent_indent = 0, len(lines), -1
    for depth, part in enumerate(parts):
        children = [i for i in range(lo, hi) if _is_content(lines[i]) and _indent(lines[i]) > parent_indent]
        child_indent = min((_indent(lines[i]) for i in children), default=parent_indent + 2)
        found = next((i for i in children if _indent(lines[i]) == child_indent and _key_of(lines[i]) == part), None)
        if found is None:
            # Insertion à la fin du bloc parent : la clé et ses sous-sections manquantes.
            insert_at = (children[-1] + 1) if children else lo
            block = []
            for extra, name in enumerate(parts[depth:]):
                pad = " " * (child_indent + 2 * extra)
                last = depth + extra == len(parts) - 1
                block.append(f"{pad}{name}: {rendered}" if last else f"{pad}{name}:")
            lines[insert_at:insert_at] = block
            return
        if depth == len(parts) - 1:
            nxt = _block_end(lines, found + 1, child_indent)
            if nxt > found + 1:
                raise YamlTextEditError(f"{dotted} est une section, pas une valeur")
            m = _VALUE_RE.match(lines[found])
            if not m:
                raise YamlTextEditError(f"ligne de {dotted} illisible")
            if isinstance(value, str) and m.group("value").strip().startswith('"'):
                rendered = json.dumps(value, ensure_ascii=False)  # style de guillemets conservé
            lines[found] = f"{m.group('head')} {rendered}{m.group('tail') or ''}"
            return
        if (lines[found].split(":", 1)[1].split("#", 1)[0]).strip():
            raise YamlTextEditError(f"{'.'.join(parts[:depth + 1])} n'est pas une section en bloc")
        lo, hi, parent_indent = found + 1, _block_end(lines, found + 1, child_indent), child_indent


def edit_yaml_text(text: str, values: dict[str, Any]) -> str:
    """Rend ``text`` avec ``values`` (clés pointées → scalaires) posées, diff minimal, vérifié."""
    try:
        original = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise YamlTextEditError(f"YAML illisible : {exc}") from exc
    if not isinstance(original, dict):
        raise YamlTextEditError("le document n'est pas un mapping YAML")
    lines = text.splitlines()
    for dotted, value in values.items():
        _set_one(lines, dotted, value)
    result = "\n".join(lines) + ("\n" if text.endswith("\n") or not text else "")
    expected = copy.deepcopy(original)
    for dotted, value in values.items():
        set_yaml_value(expected, dotted, value)
    try:
        produced = yaml.safe_load(result) or {}
    except yaml.YAMLError as exc:
        raise YamlTextEditError(f"écriture incohérente : {exc}") from exc
    if produced != expected:
        raise YamlTextEditError("écriture incohérente (style YAML non pris en charge)")
    return result


def write_text_preserving_owner(path: Path, content: str) -> None:
    """Remplace ``path`` atomiquement en CONSERVANT mode et propriétaire de l'original."""
    st = path.stat()
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        shutil.copymode(path, tmp)
        with contextlib.suppress(PermissionError):
            os.chown(tmp, st.st_uid, st.st_gid)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def set_yaml_file_scalars(path: Path, values: dict[str, Any]) -> None:
    """``edit_yaml_text`` + ``write_text_preserving_owner`` sur un fichier existant."""
    path = Path(path)
    write_text_preserving_owner(path, edit_yaml_text(path.read_text(encoding="utf-8"), values))

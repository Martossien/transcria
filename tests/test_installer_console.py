"""Tests du rendu console de l'installateur."""
from __future__ import annotations

import io

from transcria.installer.console import Console


def test_plain_output_has_no_ansi_when_color_disabled():
    buf = io.StringIO()
    console = Console(buf, color=False)
    console.info("bonjour")
    console.ok("fait")
    console.warn("attention")
    console.error("raté")
    out = buf.getvalue()
    assert "\033[" not in out
    assert "[INFO]  bonjour" in out
    assert "[OK]    fait" in out
    assert "[WARN]  attention" in out
    assert "[ERROR] raté" in out


def test_color_output_wraps_with_ansi_when_enabled():
    buf = io.StringIO()
    console = Console(buf, color=True)
    console.ok("vert")
    out = buf.getvalue()
    assert "\033[0;32m" in out and "\033[0m" in out


def test_section_renders_rule():
    buf = io.StringIO()
    console = Console(buf, color=False)
    console.section("PyTorch")
    assert "═══ PyTorch ═══" in buf.getvalue()


def test_color_auto_off_when_not_a_tty():
    # StringIO n'est pas un TTY → couleurs désactivées automatiquement (color=None).
    buf = io.StringIO()
    console = Console(buf)
    console.info("x")
    assert "\033[" not in buf.getvalue()

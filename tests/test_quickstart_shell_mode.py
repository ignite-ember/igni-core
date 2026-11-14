"""QUICKSTART's Shell Mode says which client has it.

The section described a terminal prompt changing from `>` to `$`, entered
with `!` or `$` — but `igni` ships no terminal chat UI any more (run bare,
it says so), and the composer of the desktop app and web client enters
shell mode on `$` only.
"""

from __future__ import annotations

import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _shell_mode() -> str:
    text = (_ROOT / "QUICKSTART.md").read_text(encoding="utf-8")
    start = text.index("### Shell Mode")
    return text[start : text.index("\n### ", start + 1)]


def test_shell_mode_names_the_clients_that_offer_it():
    assert "desktop app" in _shell_mode()


def test_shell_mode_is_entered_with_dollar_only():
    section = _shell_mode()

    assert "type `$`" in section
    assert "`!`" not in section
    assert "from `>` to `$`" not in section

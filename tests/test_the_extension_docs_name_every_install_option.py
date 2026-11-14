"""EXTENSIBILITY's "Developing one" names every option of ``igni ext install``.

``--replace`` was in no document: after rebuilding a page, a second
install answered "already installed … Pass --replace to upgrade it", and
nothing had said the install is a copy that only another install updates.
"""

from __future__ import annotations

from pathlib import Path

from igni_core.cli.ext import install

DOC = Path(__file__).resolve().parents[1] / "docs" / "EXTENSIBILITY.md"

#: ``--root`` points the store somewhere else, for tests and tools; a
#: developer installing an extension never needs it.
_NOT_FOR_AUTHORS = {"--root"}


def _section(text: str) -> str:
    start = text.index("### Developing one: a directory or a repository")
    return text[start : text.index("\n### ", start + 1)]


def test_every_install_option_is_named_where_installing_is_explained() -> None:
    section = _section(DOC.read_text(encoding="utf-8"))
    options = {opt for param in install.params for opt in param.opts if opt.startswith("--")}

    missing = sorted(opt for opt in options - _NOT_FOR_AUTHORS if opt not in section)
    assert not missing, f"EXTENSIBILITY's 'Developing one' does not name {missing}"


def test_the_section_says_a_change_is_installed_again_with_replace() -> None:
    section = _section(DOC.read_text(encoding="utf-8"))

    assert "igni ext install --replace" in section

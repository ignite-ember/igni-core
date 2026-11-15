"""README and QUICKSTART say what the formula and the wheels install.

They called ``ignite-ember/tap/ignite-ember`` an alias of the formula
(the tap deleted it), said the formula links both commands (it links
``igni``), and gave brew as the install everywhere, while the
``ripgrep`` dependency has wheels for macOS arm64 and Linux x86-64 only
(campaign R003).
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _ripgrep_wheels() -> set[str]:
    lock = (_ROOT / "uv.lock").read_text()
    block = lock.split('[[package]]\nname = "ripgrep"\n', 1)[1].split("[[package]]", 1)[0]
    return set(re.findall(r"ripgrep-[\d.]+-py3-none-([a-z0-9_]+)\.whl", block))


def test_ripgrep_has_wheels_for_two_platforms_only():
    wheels = _ripgrep_wheels()
    assert wheels and all(("arm64" in w and "macos" in w) or "x86_64" in w for w in wheels), wheels


def test_no_page_calls_the_deleted_formula_an_alias():
    for page in ("README.md", "QUICKSTART.md"):
        text = (_ROOT / page).read_text()
        assert "ignite-ember/tap/ignite-ember is an alias" not in text
        assert "installs\n   the same package from either" not in text
        assert "works for both)" not in text
        assert "Homebrew formula both install both names" not in text
        assert (
            "Homebrew and pip both\ninstall the `igni` command and the `ignite-ember` alias"
            not in text
        )


def test_both_pages_state_the_supported_platforms():
    for page in ("README.md", "QUICKSTART.md"):
        text = (_ROOT / page).read_text()
        assert "Apple Silicon macOS and x86-64 Linux" in text, page

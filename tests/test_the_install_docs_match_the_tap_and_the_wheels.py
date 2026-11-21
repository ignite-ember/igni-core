"""README and QUICKSTART say what the formula and the wheels install.

They called ``ignite-ember/tap/ignite-ember`` an alias of the formula
(the tap deleted it), said the formula links both commands (it links
``igni``), and gave brew as the install everywhere, while the
``ripgrep`` dependency has wheels for macOS arm64 and Linux x86-64 only
(campaign R003).

The Linux wheel must also run on the glibc the supported distributions
ship: ``ripgrep`` 15.x has only a ``manylinux_2_39`` wheel (glibc 2.39),
so on Ubuntu 22.04 (2.35), Debian 12 (2.36) or RHEL 9 (2.34) the
desktop's first-launch install fell to the sdist and had to compile it
with Rust (igni-app enterprise suite, F10).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


#: The oldest glibc among the Linux distributions igni supports (RHEL 9).
_OLDEST_GLIBC = (2, 34)


def _ripgrep_blocks() -> list[str]:
    """Each locked ``ripgrep`` version's ``[[package]]`` block."""
    lock = (_ROOT / "uv.lock").read_text()
    return [
        part.split("[[package]]", 1)[0]
        for part in lock.split('[[package]]\nname = "ripgrep"\n')[1:]
    ]


def _ripgrep_wheels() -> set[str]:
    return {
        tag
        for block in _ripgrep_blocks()
        for tag in re.findall(r"ripgrep-[\d.]+-py3-none-([a-z0-9_]+)\.whl", block)
    }


def test_ripgrep_has_wheels_for_two_platforms_only():
    wheels = _ripgrep_wheels()
    assert wheels and all(("arm64" in w and "macos" in w) or "x86_64" in w for w in wheels), wheels


def test_the_linux_ripgrep_installs_from_a_wheel_on_the_oldest_supported_glibc():
    linux = []
    for block in _ripgrep_blocks():
        markers = re.search(r"resolution-markers = \[(.*?)\]", block, re.S)
        if markers and "linux" not in markers.group(1):
            continue
        linux.append(block)
        floors = [(int(a), int(b)) for a, b in re.findall(r"manylinux_(\d+)_(\d+)_x86_64", block)]
        assert floors and min(floors) <= _OLDEST_GLIBC, (block.splitlines()[0], floors)
    assert linux


def test_the_linux_ripgrep_is_pinned_to_one_version():
    """A range lets pip and uv pick a newer release whose only Linux file
    that fits is the sdist: they take the newest version that installs at
    all, and an sdist installs, by compiling."""
    deps = tomllib.loads((_ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    (linux,) = [d for d in deps if d.startswith("ripgrep") and "linux" in d]
    assert re.match(r"ripgrep==[\d.]+;", linux), linux


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

"""The docs may only tell a reader to install what this repository builds.

`igni-core` has never been published to PyPI: the name is a 404. The
`ignite-ember` name on PyPI is the **pre-split monolith** — v1.0.3, the
`ember_code` module — so the command is the same and the product behind
it is the old one. A reader who followed `pip install igni-core` got
nothing; a reader who followed `pip install ignite-ember` got the old
product with no word about it.

Three rules, stated over the markdown files rather than over a list
somebody audited:

1. every package a fenced install command installs is one this
   repository builds (`igni-core`, `igni-cloud`, `ignite-ember`), the
   checkout itself (`-e ".[...]"`), or a named third-party package;
2. a fenced `git clone` is followed by a `cd` into the directory that
   clone creates — `git clone .../igni-core.git` creates `igni-core/`,
   and three documents said `cd igni`;
3. while the published package holds the pre-split release, the README
   and the quickstart say so, and name the version.

Rule 3 is the one that fires when the release lands: it demands the
note come out of the docs, and the note is the only place that says
what a reader would actually get.
"""

from __future__ import annotations

import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DOCS = sorted((_ROOT / "docs").glob("*.md")) + sorted(_ROOT.glob("*.md"))

#: The distributions this repository builds. `packages/ignite-ember/` is
#: the alias: metadata, and a dependency on `igni-core` at the same
#: version.
_BUILT = {"igni-core", "igni-cloud", "ignite-ember"}

#: Third-party packages the docs tell a reader to install. Each one is
#: named on purpose, so a new name has to be argued for.
_THIRD_PARTY = {"pypi-attestations"}

_SHELL_FENCE = re.compile(r"```(?:bash|sh|shell|console)\n(.*?)```", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_CLONE = re.compile(r"git clone\s+(\S+)")
_CD = re.compile(r"^\s*cd\s+(\S+)")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: The phrase the install note carries, and the version it names. The
#: release that publishes `igni-core` changes both — and this test is
#: what says so.
_NOTE = "last pre-split release"
_NOTE_VERSION = "1.0.3"


def _shell_blocks(doc: pathlib.Path) -> list[str]:
    return _SHELL_FENCE.findall(doc.read_text(encoding="utf-8"))


def _install_lines_everywhere(doc: pathlib.Path) -> list[str]:
    """Every install command in a document: the fenced ones and the ones
    a sentence names in a code span."""
    lines = []
    for block in _shell_blocks(doc):
        lines.extend(_install_lines(block))
    for span in _INLINE_CODE.findall(doc.read_text(encoding="utf-8")):
        if "pip install" in span:
            lines.append(span.strip())
    return lines


def _install_lines(block: str) -> list[str]:
    lines = []
    for line in block.splitlines():
        line = line.split(" #")[0].strip()
        if "pip install" in line:
            lines.append(line)
    return lines


def _packages(command: str) -> list[str]:
    """The distribution names a `pip install` line installs."""
    tail = command.split("pip install", 1)[1]
    names = []
    for token in tail.split():
        token = token.strip("\"'")
        if token.startswith(("-", ".", "@")) or "://" in token:
            continue
        name = re.split(r"[\[<>=!~;]", token, maxsplit=1)[0]
        if name and _NAME.match(name):
            names.append(name)
    return names


@pytest.mark.parametrize("doc", _DOCS, ids=lambda d: d.name)
def test_every_install_command_names_a_package_this_repo_builds(doc):
    problems = []
    for line in _install_lines_everywhere(doc):
        for name in _packages(line):
            if name not in _BUILT and name not in _THIRD_PARTY:
                problems.append(f"{name} (in `{line}`)")
    assert not problems, (
        f"{doc.name} installs {sorted(set(problems))}. This repository builds "
        f"{sorted(_BUILT)}; anything else is either the checkout itself "
        f'(`pip install -e ".[...]"`) or a third-party package that has to '
        f"be added to the allowlist on purpose."
    )


@pytest.mark.parametrize("doc", _DOCS, ids=lambda d: d.name)
def test_a_clone_is_followed_by_a_cd_into_the_directory_it_creates(doc):
    problems = []
    for block in _shell_blocks(doc):
        cloned = None
        for line in block.splitlines():
            clone = _CLONE.search(line)
            if clone:
                cloned = clone.group(1).rstrip("/").rsplit("/", 1)[-1]
                cloned = cloned.removesuffix(".git")
                continue
            cd = _CD.match(line)
            if cd and cloned is not None:
                if cd.group(1) != cloned:
                    problems.append(f"cloned {cloned}, then `cd {cd.group(1)}`")
                cloned = None
    assert not problems, (
        f"{doc.name} has {problems}. `git clone <url>` creates the directory "
        f"named at the end of the url, and the `cd` has to name it."
    )


@pytest.mark.parametrize("name", ["README.md", "QUICKSTART.md"])
def test_the_install_note_says_what_is_published_today(name):
    """The published package holds the pre-split release; the docs have to
    say so, or a reader installs the old product without knowing."""
    text = (_ROOT / name).read_text(encoding="utf-8")
    assert _NOTE in text, (
        f"{name} does not say that the published package is the {_NOTE}. "
        f"It should, until igni-core is on PyPI."
    )
    assert _NOTE_VERSION in text, f"{name} does not name the published version."

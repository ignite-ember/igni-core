"""The docs may only tell a reader to install what this repository builds.

`igni-core` is on PyPI from v1.0.4, and it is this repository's build:
the `igni_core` module, the `igni` command, the decisions service. The
`ignite-ember` name on PyPI is the alias — the same version, the same
extras, nothing of its own — so `pip install ignite-ember` installs
`igni-core` and `pip install igni-core` installs the same thing.

Three rules, stated over the markdown files rather than over a list
somebody audited:

1. every package a fenced install command installs is one this
   repository builds (`igni-core`, `igni-cloud`, `ignite-ember`), the
   checkout itself (`-e ".[...]"`), or a named third-party package;
2. a fenced `git clone` is followed by a `cd` into the directory that
   clone creates — `git clone .../igni-core.git` creates `igni-core/`,
   and three documents said `cd igni`;
3. the README and the quickstart say what is published today and name
   its version.

Rule 3 is the one that fires when a release lands: the note about the
pre-split package comes out of the docs, and the version that replaced
it takes its place. A reader who followed the docs has to be able to
tell which build they would get.
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

#: The phrase the install note carried while the published package was
#: the pre-split release. It has to be gone now: the note said the
#: published thing was not this repository's build, and that stopped
#: being true when `igni-core` reached PyPI.
_NOTE = "last pre-split release"

#: What is published now, and the version it is at. The next release
#: changes this — and this test is what says so.
_PUBLISHED = "igni-core"
_PUBLISHED_VERSION = "1.0.4"


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
    """`igni-core` is published; the docs have to say so and name the
    version, or a reader cannot tell what they would install."""
    text = (_ROOT / name).read_text(encoding="utf-8")
    assert _NOTE not in text, (
        f"{name} still says the published package is the {_NOTE}. Since "
        f"{_PUBLISHED_VERSION} that is not true: {_PUBLISHED} is on PyPI and "
        f"holds this repository's build."
    )
    assert _PUBLISHED in text, (
        f"{name} does not name {_PUBLISHED}, the package PyPI carries today."
    )
    assert _PUBLISHED_VERSION in text, f"{name} does not name the published version."

"""Every managed-settings path the docs give is one igni reads.

SECURITY.md gave the Linux file as ``/etc/ignite-ember/managed-settings.json``
while igni reads ``/etc/igni/managed-settings.yaml``. An administrator who
followed it got no policy at all — extensions ran unsandboxed with
``require_sandbox`` written in the wrong file (campaign R311). Managed
policy is the tier a user may not override, so "silently not read" is the
worst way for it to fail.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_DOCS = sorted((_ROOT / "docs").glob("*.md")) + [_ROOT / "README.md"]

#: The managed file under ``core.paths.managed_policy_dir`` on each platform,
#: and the bare name the docs use when they mean "that file".
_READ = {
    "/Library/Application Support/igni/managed-settings.yaml",
    "/etc/igni/managed-settings.yaml",
    "%PROGRAMDATA%\\igni\\managed-settings.yaml",
    "managed-settings.yaml",
}


def _mentions() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    pattern = re.compile(
        r"(/Library/Application Support/[^\s`]*managed-settings\.\w+"
        r"|%PROGRAMDATA%[^\s`]*managed-settings\.\w+"
        r"|[^\s`(\"']*managed-settings\.\w+)"
    )
    for doc in _DOCS:
        for match in pattern.finditer(doc.read_text()):
            found.append((doc.name, match.group(1)))
    return found


def test_the_docs_mention_the_managed_file():
    assert len(_mentions()) >= 5


@pytest.mark.parametrize(("doc", "path"), _mentions())
def test_the_path_is_one_igni_reads(doc: str, path: str):
    # A /run/... path is a test or container mount the page explains.
    if path.startswith("/run/"):
        return
    assert path in _READ, f"{doc} names {path!r}; igni reads only {sorted(_READ)}"


def test_the_platform_directories_are_the_ones_the_code_uses(monkeypatch):
    from igni_core.core import paths

    monkeypatch.setattr(paths.sys, "platform", "linux")
    assert str(paths.managed_policy_dir()) == "/etc/igni"

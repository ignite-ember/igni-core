"""``igni ext install`` prints what an extension may use, and the workspace
lines agree with each other.

Under ``workspace none`` the summary went on with ``workspace paths  all``,
which reads as a grant of every path; ``workspacePaths`` only narrows a
workspace that is granted.
"""

from __future__ import annotations

import pytest

from igni_core.cli.ext import _echo_grants
from igni_core.extensions.manifest import ExtensionManifest


def _manifest(**permissions) -> ExtensionManifest:
    return ExtensionManifest.model_validate(
        {
            "id": "acme.tickets",
            "version": "0.2.0",
            "contract": ">=1.0 <2",
            "name": "Ticket lookup",
            "trust": "unsigned",
            "dev": {"command": ["python", "server.py"]},
            "permissions": permissions,
        }
    )


def _lines(capsys: pytest.CaptureFixture[str], manifest: ExtensionManifest) -> list[str]:
    _echo_grants(manifest)
    return [line.strip() for line in capsys.readouterr().out.splitlines()]


def test_no_workspace_prints_no_paths(capsys):
    lines = _lines(capsys, _manifest(workspace="none"))

    assert "workspace        none" in lines
    assert not [line for line in lines if line.startswith("workspace paths")]


def test_a_granted_workspace_with_no_paths_is_the_whole_workspace(capsys):
    lines = _lines(capsys, _manifest(workspace="read"))

    assert "workspace paths  the whole workspace" in lines


def test_a_granted_workspace_lists_its_paths(capsys):
    lines = _lines(capsys, _manifest(workspace="read", workspacePaths=["docs/**", "*.md"]))

    assert "workspace paths  docs/**, *.md" in lines

"""A real session, built the way the tests that need one build it.

Not a test module: no ``test_`` prefix, so pytest collects nothing.

A :class:`~igni_core.core.session.core.Session` reaches the disk and the
network while it composes itself — the plugin roots, the project
context, the cloud catalog — so a test that wants one gives it a
throwaway project with a stub model registry and a throwaway data
directory, and gets a session that composes itself without a cloud
connection.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from igni_core.core.config.settings import Settings
from igni_core.core.config.settings_loader import SettingsLoader
from igni_core.core.mcp.hints import ToolHintsRegistry
from igni_core.core.paths import CONFIG_DIR

if TYPE_CHECKING:
    from igni_core.core.session.core import Session

#: Enough of a model registry for a Session to construct.
STUB_MODEL: dict[str, Any] = {
    "default": "stub",
    "registry": {
        "stub": {
            "provider": "openai_like",
            "model_id": "stub-1",
            "url": "http://localhost:9/v1",
            "api_key": "x",
            "context_window": 8192,
        }
    },
}


def stub_project(path: Path) -> Path:
    """A git repository with an igni config that has a stub model."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "f.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"],
        cwd=path,
        check=True,
    )
    return path


def stub_settings(project: Path, *, home: Path) -> Settings:
    """Settings for *project*, with a model registry a session can resolve."""
    config = project / CONFIG_DIR
    config.mkdir(parents=True, exist_ok=True)
    (config / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "code_index": {"enabled": False},
                "knowledge": {"enabled": False},
                "models": STUB_MODEL,
                "storage": {"data_dir": str(home)},
            }
        )
    )
    return SettingsLoader.load(project_dir=project)


def build_session(root: Path, *, tool_hints: ToolHintsRegistry | None = None) -> Session:
    """A session on a throwaway project under *root*."""
    from igni_core.core.session.core import Session

    project = stub_project(root / "proj")
    return Session(
        settings=stub_settings(project, home=root / "home"),
        project_dir=project,
        tool_hints=tool_hints,
    )


__all__ = ["STUB_MODEL", "build_session", "stub_project", "stub_settings"]

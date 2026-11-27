"""Bypass asks before the agent reads igni's own key stores.

``~/.igni/config.yaml`` holds the model registry's API keys and
``~/.igni/credentials.json`` the cloud sign-in token. In a real Bypass
session with only the rules deciding (the safety model was not
installed), the agent ran ``sqlite3`` and ``grep`` across ``~/.igni``
unasked; nothing in the rules named these two files, so ``cat
~/.igni/config.yaml`` would have run the same way. A file read of
``config.yaml`` was not flagged either.

These go through the real guard, rules only, and the person declines.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from igni_core.core.config.schemas.safety import BypassGuardConfig
from igni_core.core.hooks.bypass_guard import NOT_RUN, BypassGuard
from igni_core.core.mcp.hints import ToolHintsRegistry


@pytest.fixture
def home() -> Path:
    """The test's home directory, which the suite's conftest points ``HOME`` at."""
    return Path.home()


def _check(project: Path, tool: str, args: dict[str, Any]) -> tuple[str | None, list[str]]:
    asked: list[str] = []

    async def ask(
        name: str, call_args: dict[str, Any], prompt: str, path: list[str], **_: Any
    ) -> bool:
        asked.append(prompt)
        return False

    guard = BypassGuard(
        config=BypassGuardConfig(use_model=False, explain=False),
        project_dir=project,
        in_bypass=lambda: True,
        ask=ask,
        tool_hints=ToolHintsRegistry(),
    )
    return asyncio.run(guard.check(tool, args)), asked


@pytest.mark.parametrize(
    "command",
    [
        "cat ~/.igni/config.yaml",
        "head -20 $HOME/.igni/config.yaml",
        "cat ~/.igni/credentials.json",
        "cat ~/.igni/config.yaml.bak-before-m3.1-flash",
        "grep api_key ~/.ember/config.yaml",
    ],
)
def test_a_shell_read_of_an_igni_key_store_is_put_to_the_person(home, tmp_path, command):
    result, asked = _check(tmp_path, "run_shell_command", {"command": command})

    assert len(asked) == 1
    assert result is not None and result.startswith(NOT_RUN)


def test_the_absolute_home_path_is_seen_too(home, tmp_path):
    command = f"cat {home}/.igni/config.yaml"
    result, asked = _check(tmp_path, "run_shell_command", {"command": command})
    assert asked and result is not None


@pytest.mark.parametrize(
    "path", ["~/.igni/config.yaml", "{home}/.igni/credentials.json", "{home}/.igni/config.yaml.bak"]
)
def test_a_file_read_of_an_igni_key_store_is_put_to_the_person(home, tmp_path, path):
    result, asked = _check(tmp_path, "read_file", {"file_path": path.format(home=home)})
    assert asked and result is not None


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("run_shell_command", {"command": "cat .igni/config.yaml"}),
        ("read_file", {"file_path": "{project}/.igni/config.yaml"}),
        ("run_shell_command", {"command": "ls -la ~/.igni"}),
    ],
)
def test_a_project_config_and_a_listing_are_ordinary_work(home, tmp_path, tool, args):
    filled = {k: v.format(project=tmp_path) for k, v in args.items()}
    result, asked = _check(tmp_path, tool, filled)
    assert (result, asked) == (None, [])

"""What the CLI stashes for its subcommands is typed, optional where it
may be absent, and cannot be changed after it was built.

A subcommand reads ``ctx.obj.settings`` by attribute, so a payload whose
shape drifts has to fail loudly, and one a subcommand could rewrite would
let one command change what the next one reads.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from igni_core.cli.context import CliContext
from igni_core.core.config.settings import Settings
from igni_core.core.worktree import WorktreeManager


def test_only_the_settings_are_required_and_the_rest_default_to_none():
    settings = Settings()
    context = CliContext(settings=settings)
    assert context.settings is settings
    assert context.worktree_manager is None
    assert context.project_dir is None
    assert context.additional_dirs is None


def test_a_context_without_settings_is_refused():
    with pytest.raises(ValidationError, match="settings"):
        CliContext()  # type: ignore[call-arg]


def test_a_context_cannot_be_changed_once_built():
    context = CliContext(settings=Settings())
    with pytest.raises(ValidationError, match="frozen"):
        context.project_dir = Path("/elsewhere")  # type: ignore[misc]
    with pytest.raises(ValidationError, match="frozen"):
        context.settings = Settings()  # type: ignore[misc]


def test_a_worktree_manager_that_is_not_one_is_refused(tmp_path):
    with pytest.raises(ValidationError, match="worktree_manager"):
        CliContext(settings=Settings(), worktree_manager=object())  # type: ignore[arg-type]
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    manager = WorktreeManager(tmp_path)
    assert CliContext(settings=Settings(), worktree_manager=manager).worktree_manager is manager


def test_directories_arrive_as_paths_even_when_given_as_text(tmp_path):
    context = CliContext(
        settings=Settings(),
        project_dir=str(tmp_path),  # type: ignore[arg-type]
        additional_dirs=[str(tmp_path / "a")],  # type: ignore[list-item]
    )
    assert context.project_dir == tmp_path
    assert context.additional_dirs == [tmp_path / "a"]
    assert all(isinstance(path, Path) for path in context.additional_dirs or [])


def test_settings_that_are_not_settings_are_refused():
    with pytest.raises(ValidationError, match="settings"):
        CliContext(settings=42)  # type: ignore[arg-type]


def test_an_unknown_attribute_is_an_error_not_a_missing_key():
    context = CliContext(settings=Settings())
    with pytest.raises(AttributeError):
        context.worktree  # type: ignore[attr-defined]  # noqa: B018

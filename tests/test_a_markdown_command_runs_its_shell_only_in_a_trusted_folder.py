"""A markdown command's shell tokens and the files it is read from.

The folder's own commands run their ``!`cmd``` tokens only once the
folder is trusted; the user's commands are not the folder's. Real files in
a temp directory and the real trust service, starting undecided.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from igni_core.core.paths import CONFIG_DIR
from igni_core.core.utils.markdown_commands import MarkdownCommand
from igni_core.core.workspace_trust import process_trust

pytestmark = pytest.mark.workspace_trust


def _project_command(tmp_path: Path, body: str) -> tuple[Path, MarkdownCommand]:
    project = tmp_path / "project"
    path = project / CONFIG_DIR / "commands" / "go.md"
    path.parent.mkdir(parents=True)
    path.write_text(body)
    command = MarkdownCommand.load(path)
    assert command is not None
    return project, command


@pytest.mark.asyncio
async def test_a_project_command_in_an_untrusted_folder_does_not_run_its_shell(tmp_path):
    project, command = _project_command(tmp_path, "Before !`echo RANVALUE` after")

    out = await command.render("", project_dir=project)

    assert "RANVALUE" not in out
    assert "not run" in out
    assert out.startswith("Before ")
    assert out.endswith(" after")


@pytest.mark.asyncio
async def test_the_same_command_runs_its_shell_once_the_folder_is_trusted(tmp_path):
    project, command = _project_command(tmp_path, "Before !`echo RANVALUE` after")
    process_trust().trust(project, remember=False)

    out = await command.render("", project_dir=project)

    assert out == "Before RANVALUE after"


@pytest.mark.asyncio
async def test_a_user_command_runs_its_shell_whatever_the_folder_decided(tmp_path):
    user_file = tmp_path / "home" / CONFIG_DIR / "commands" / "mine.md"
    user_file.parent.mkdir(parents=True)
    user_file.write_text("!`echo MINE`")
    command = MarkdownCommand.load(user_file)
    assert command is not None
    project = tmp_path / "project"
    project.mkdir()

    assert await command.render("", project_dir=project) == "MINE"


@pytest.mark.asyncio
async def test_a_command_with_no_project_folder_runs_its_shell(tmp_path):
    path = tmp_path / "x.md"
    path.write_text("!`echo FREE`")
    command = MarkdownCommand.load(path)
    assert command is not None

    assert await command.render("") == "FREE"


def test_only_md_files_are_commands(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    root = tmp_path / "project" / CONFIG_DIR / "commands"
    root.mkdir(parents=True)
    (root / "real.md").write_text("body")
    (root / "notes.txt").write_text("not a command")
    (root / "script.sh").write_text("echo")
    (root / "folder.md").mkdir()

    assert set(MarkdownCommand.discover(tmp_path / "project")) == {"real"}

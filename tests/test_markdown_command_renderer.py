"""Turning a markdown command body into the prompt the agent receives.

The renderer owns three token kinds and one safety property: the
order. ``$ARGUMENTS`` goes in last, so the text a model typed can
never become a shell command or a file reference. These tests pin
that ordering, and the ways a token can fail — a shell that exits
non-zero, a shell that hangs, a path that is not there, a file that
cannot be read.

The renderer is what decides whether a project's own shell tokens run
at all, so the trust decision is a constructor argument here: the
renderer does not ask whether a folder is trusted, it is told.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from igni_core.core.utils import markdown_command_renderer as renderer_module
from igni_core.core.utils.markdown_command_renderer import MarkdownCommandRenderer


def _renderer(
    source: Path,
    *,
    project_dir: Path | None = None,
    shell_allowed: bool = True,
) -> MarkdownCommandRenderer:
    return MarkdownCommandRenderer(
        source=source,
        project_dir=project_dir,
        shell_allowed=shell_allowed,
    )


# ── A body with nothing to expand ─────────────────────────────


class TestAPlainBody:
    @pytest.mark.asyncio
    async def test_a_body_without_tokens_is_unchanged(self, tmp_path):
        out = await _renderer(tmp_path / "cmd.md").render("Static body.", "args")

        assert out == "Static body."

    @pytest.mark.asyncio
    async def test_the_arguments_go_in_last(self, tmp_path):
        """``$ARGUMENTS`` is substituted into the *expanded* body, so
        an argument that looks like a token stays text."""
        out = await _renderer(tmp_path / "cmd.md").render("Args: $ARGUMENTS", "!`echo hi`")

        assert out == "Args: !`echo hi`"


# ── Shell tokens ──────────────────────────────────────────────


class TestTheShellTokens:
    @pytest.mark.asyncio
    async def test_the_command_output_is_inlined(self, tmp_path):
        out = await _renderer(tmp_path / "cmd.md").render("Echo: !`echo hello` done", "")

        assert out == "Echo: hello done"

    @pytest.mark.asyncio
    async def test_the_command_runs_in_the_project(self, tmp_path):
        """The shell's working directory is the project — a command
        that reads a file reads the project's file."""
        (tmp_path / "marker.txt").write_text("HERE")

        out = await _renderer(
            tmp_path / "cmd.md",
            project_dir=tmp_path,
        ).render("!`cat marker.txt`", "")

        assert out == "HERE"

    @pytest.mark.asyncio
    async def test_without_a_project_the_command_file_is_the_working_directory(self, tmp_path):
        (tmp_path / "marker.txt").write_text("HERE")

        out = await _renderer(tmp_path / "cmd.md").render("!`cat marker.txt`", "")

        assert out == "HERE"

    @pytest.mark.asyncio
    async def test_the_command_runs_in_the_project_wherever_the_file_lives(self, tmp_path):
        """The command file's own directory is not the working
        directory — the project is. A command file kept elsewhere
        still reads the project's files."""
        project = tmp_path / "project"
        project.mkdir()
        (project / "marker.txt").write_text("HERE")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()

        out = await _renderer(
            elsewhere / "cmd.md",
            project_dir=project,
        ).render("!`cat marker.txt`", "")

        assert out == "HERE"

    @pytest.mark.asyncio
    async def test_several_tokens_run_together(self, tmp_path):
        out = await _renderer(tmp_path / "cmd.md").render(
            "A=!`echo first` B=!`echo second`",
            "",
        )

        assert out == "A=first B=second"

    @pytest.mark.asyncio
    async def test_a_failing_command_says_so(self, tmp_path):
        """An empty expansion would leave the agent working from
        nothing; the marker tells it the command failed."""
        out = await _renderer(tmp_path / "cmd.md").render("!`false`", "")

        assert "[error:" in out
        assert "exit 1" in out

    @pytest.mark.asyncio
    async def test_a_command_that_cannot_start_says_so(self, tmp_path):
        """A working directory that is not there is an OSError before
        the shell ever runs."""
        out = await _renderer(tmp_path / "gone" / "cmd.md").render("!`echo hi`", "")

        assert "[error:" in out

    @pytest.mark.asyncio
    async def test_a_command_that_hangs_is_stopped(self, tmp_path, monkeypatch):
        """A shell that never returns must not hold the turn."""
        monkeypatch.setattr(renderer_module, "_SHELL_TIMEOUT_SECONDS", 0.05)

        out = await _renderer(tmp_path / "cmd.md").render("!`sleep 30`", "")

        assert "[error:" in out
        assert "timed out" in out

    @pytest.mark.asyncio
    async def test_the_arguments_go_into_the_shell_as_one_word(self, tmp_path):
        """``$ARGUMENTS`` inside a shell token is quoted, so the
        argument text is never shell syntax."""
        out = await _renderer(tmp_path / "cmd.md").render("!`echo $ARGUMENTS`", "a b; c")

        assert out == "a b; c"

    @pytest.mark.asyncio
    async def test_an_untrusted_command_does_not_run_its_shell(self, tmp_path):
        """A command from an untrusted folder still renders — but its
        shell tokens are replaced, not run."""
        out = await _renderer(
            tmp_path / "cmd.md",
            shell_allowed=False,
        ).render("Before !`echo RANVALUE` after", "")

        assert "Before " in out
        assert "after" in out
        assert "RANVALUE" not in out
        assert "not run" in out

    @pytest.mark.asyncio
    async def test_an_unexpected_failure_in_a_shell_token_propagates(self, tmp_path, monkeypatch):
        """A shell substitution that fails in a way nobody planned for
        is a bug in the render. It is not caught and turned into a
        marker — the marker would hide the bug behind a plausible
        looking line of prompt."""
        from igni_core.core.utils.markdown_command_renderer import MarkdownCommandRenderer

        async def _boom(self, cmd, *, cwd):
            raise RuntimeError("shell exploded")

        monkeypatch.setattr(MarkdownCommandRenderer, "_run_shell", _boom)

        with pytest.raises(RuntimeError):
            await _renderer(tmp_path / "cmd.md").render("!`echo hi`", "")


# ── File tokens ───────────────────────────────────────────────


class TestTheFileTokens:
    @pytest.mark.asyncio
    async def test_a_relative_path_resolves_against_the_command_file(self, tmp_path):
        (tmp_path / "data.txt").write_text("DATA")

        out = await _renderer(tmp_path / "cmd.md").render("Read: @./data.txt", "")

        assert out == "Read: DATA"

    @pytest.mark.asyncio
    async def test_an_absolute_path_is_read(self, tmp_path):
        target = tmp_path / "data.txt"
        target.write_text("DATA")

        out = await _renderer(tmp_path / "cmd.md").render(f"Read: @{target}", "")

        assert out == "Read: DATA"

    @pytest.mark.asyncio
    async def test_a_home_path_is_expanded(self, tmp_path, monkeypatch):
        """``@~/notes.md`` means the user's own notes."""
        home = tmp_path / "home"
        home.mkdir()
        (home / "notes.md").write_text("NOTES")
        monkeypatch.setenv("HOME", str(home))

        out = await _renderer(tmp_path / "cmd.md").render("Read: @~/notes.md", "")

        assert out == "Read: NOTES"

    @pytest.mark.asyncio
    async def test_trailing_punctuation_stays_out_of_the_path(self, tmp_path):
        (tmp_path / "README.md").write_text("README")

        out = await _renderer(tmp_path / "cmd.md").render("See @./README.md.", "")

        assert out == "See README."

    @pytest.mark.asyncio
    async def test_a_path_that_is_not_there_stays_a_literal(self, tmp_path):
        out = await _renderer(tmp_path / "cmd.md").render("See @./nope.md", "")

        assert out == "See @./nope.md"

    @pytest.mark.asyncio
    async def test_a_directory_stays_a_literal(self, tmp_path):
        (tmp_path / "adir").mkdir()

        out = await _renderer(tmp_path / "cmd.md").render("See @./adir", "")

        assert out == "See @./adir"

    @pytest.mark.asyncio
    async def test_a_file_that_cannot_be_read_stays_a_literal(self, tmp_path):
        """A file the process cannot decode is left as the token, so
        the agent sees the broken reference instead of a traceback."""
        (tmp_path / "broken.txt").write_bytes(b"\xff\xfe\x00not text")

        out = await _renderer(tmp_path / "cmd.md").render("See @./broken.txt", "")

        assert out == "See @./broken.txt"


# ── The project boundary ───────────────────────────────────────


class TestTheProjectBoundary:
    @pytest.mark.asyncio
    async def test_a_project_command_may_read_inside_the_project(self, tmp_path):
        """The boundary blocks the way out, not the way in."""
        project = tmp_path / "project"
        project.mkdir()
        (project / "inside.txt").write_text("INSIDE")

        out = await _renderer(
            project / "cmd.md",
            project_dir=project,
        ).render("Read: @./inside.txt", "")

        assert out == "Read: INSIDE"

    @pytest.mark.asyncio
    async def test_a_project_command_cannot_read_outside_the_project(self, tmp_path):
        """A command file living inside a project may not reference a
        path outside it."""
        project = tmp_path / "project"
        project.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("SECRET")

        out = await _renderer(
            project / "cmd.md",
            project_dir=project,
        ).render(f"Read: @{outside}", "")

        assert "SECRET" not in out
        assert str(outside) in out

    @pytest.mark.asyncio
    async def test_a_command_outside_the_project_may_read_anything(self, tmp_path):
        """A user-tier command was written by the user; it is not the
        project's to police."""
        outside = tmp_path / "outside.txt"
        outside.write_text("CONTENT")
        user_cmd = tmp_path / "home" / "cmd.md"
        user_cmd.parent.mkdir()
        user_cmd.write_text("body")

        out = await _renderer(
            user_cmd,
            project_dir=tmp_path / "project",
        ).render(f"Read: @{outside}", "")

        assert "CONTENT" in out

    @pytest.mark.asyncio
    async def test_without_a_project_everything_is_readable(self, tmp_path):
        """No project to be bound by — the command is the user's own."""
        outside = tmp_path / "outside.txt"
        outside.write_text("CONTENT")

        out = await _renderer(tmp_path / "cmd.md").render(f"Read: @{outside}", "")

        assert "CONTENT" in out

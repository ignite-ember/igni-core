"""Tests for the markdown-authored custom commands subsystem.

Covers discovery (multi-root precedence, ``read_claude`` toggle),
frontmatter parsing (description / allowed-tools / argument-hint /
model — including malformed YAML), and the three template-token
substitutions (``$ARGUMENTS``, ``!`cmd```, ``@path``)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from igni_core.backend.command_handler import CommandHandler
from igni_core.core.paths import CONFIG_DIR
from igni_core.core.utils.markdown_command_frontmatter import CommandFrontmatter
from igni_core.core.utils.markdown_commands import MarkdownCommand
from igni_core.protocol.messages import CommandAction


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


# ── Frontmatter parsing ────────────────────────────────────────


class TestParseFrontmatter:
    def test_no_frontmatter_returns_empty_meta(self):
        meta, body = CommandFrontmatter.parse("Just a body, no header.\n")
        assert meta == CommandFrontmatter()
        assert body == "Just a body, no header.\n"

    def test_simple_frontmatter(self):
        meta, body = CommandFrontmatter.parse(
            "---\ndescription: Quick status\nmodel: sonnet\n---\nBody.\n"
        )
        assert meta.description == "Quick status"
        assert meta.model == "sonnet"
        assert body == "Body.\n"

    def test_malformed_yaml_is_no_op(self):
        """A broken frontmatter block shouldn't sink the whole
        command — fail open and treat the file as bodyless meta."""
        meta, body = CommandFrontmatter.parse("---\n: : not valid:\n  :\n---\nBody survives.\n")
        assert meta == CommandFrontmatter()
        assert "Body survives" in body

    def test_non_dict_yaml_treated_as_empty(self):
        """A scalar / list at the top level isn't a meta dict."""
        meta, body = CommandFrontmatter.parse("---\njust a string\n---\nBody.\n")
        assert meta == CommandFrontmatter()
        assert body == "Body.\n"


# ── Discovery ─────────────────────────────────────────────────


class TestDiscover:
    def test_finds_project_igni_command(self, tmp_path, monkeypatch):
        """Most basic case: a single command at
        ``<project>/.igni/commands/review.md``."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        (tmp_path / "home").mkdir()
        _write(
            tmp_path / CONFIG_DIR / "commands" / "review.md",
            "---\ndescription: Review the diff\n---\nReview changes.\n",
        )
        cmds = MarkdownCommand.discover(tmp_path)
        assert "review" in cmds
        assert cmds["review"].description == "Review the diff"
        assert "Review changes." in cmds["review"].body

    def test_finds_user_global_command(self, tmp_path, monkeypatch):
        """A command at ``~/.igni/commands/`` is available in any
        project — covers the global-tier discovery path."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        _write(home / CONFIG_DIR / "commands" / "global.md", "Globally available.\n")
        cmds = MarkdownCommand.discover(tmp_path)
        assert "global" in cmds

    def test_project_overrides_user_global_on_name_collision(self, tmp_path, monkeypatch):
        """Project commands win — drop-in user globals shouldn't
        silently shadow a project-defined behavior."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        _write(home / CONFIG_DIR / "commands" / "review.md", "GLOBAL\n")
        _write(tmp_path / CONFIG_DIR / "commands" / "review.md", "PROJECT\n")
        cmds = MarkdownCommand.discover(tmp_path)
        assert "PROJECT" in cmds["review"].body
        assert "GLOBAL" not in cmds["review"].body

    def test_igni_dir_beats_claude_dir_at_same_tier(self, tmp_path, monkeypatch):
        """Within the same tier (e.g. both project), igni wins —
        keeps our own namespace authoritative when both exist."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        _write(tmp_path / ".claude" / "commands" / "review.md", "FROM-CLAUDE\n")
        _write(tmp_path / CONFIG_DIR / "commands" / "review.md", "FROM-IGNI\n")
        cmds = MarkdownCommand.discover(tmp_path)
        assert "FROM-IGNI" in cmds["review"].body

    def test_read_claude_false_skips_claude_dirs(self, tmp_path, monkeypatch):
        """``cross_tool_support=False`` → only igni dirs scanned.
        A user with this toggle off shouldn't get drive-by command
        injection from ``~/.claude/commands/``."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        _write(home / ".claude" / "commands" / "claude.md", "Should-not-load")
        _write(tmp_path / ".claude" / "commands" / "proj.md", "Should-not-load")
        _write(tmp_path / CONFIG_DIR / "commands" / "igni.md", "Loads")
        cmds = MarkdownCommand.discover(tmp_path, read_claude=False)
        assert "claude" not in cmds
        assert "proj" not in cmds
        assert "igni" in cmds

    def test_a_project_claude_command_is_found_when_the_toggle_is_on(self, tmp_path, monkeypatch):
        """The cross-tool sources are read when the user consents."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        _write(tmp_path / ".claude" / "commands" / "cc_proj.md", "CC PROJECT\n")

        cmds = MarkdownCommand.discover(tmp_path, read_claude=True)

        assert "CC PROJECT" in cmds["cc_proj"].body

    def test_a_user_claude_command_is_found_when_the_toggle_is_on(self, tmp_path, monkeypatch):
        """The user tier's cross-tool source is read too."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        _write(home / ".claude" / "commands" / "cc_user.md", "CC USER\n")

        cmds = MarkdownCommand.discover(tmp_path, read_claude=True)

        assert "CC USER" in cmds["cc_user"].body

    def test_dotfiles_ignored(self, tmp_path, monkeypatch):
        """Editor backup files like ``.review.md.swp`` shouldn't
        accidentally register as a command."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        _write(tmp_path / CONFIG_DIR / "commands" / ".backup.md", "Should not load")
        cmds = MarkdownCommand.discover(tmp_path)
        assert cmds == {}

    def test_allowed_tools_parsed_as_list(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        _write(
            tmp_path / CONFIG_DIR / "commands" / "rev.md",
            "---\nallowed-tools: Bash, Read,Write \n---\nBody\n",
        )
        cmds = MarkdownCommand.discover(tmp_path)
        assert cmds["rev"].allowed_tools == ("Bash", "Read", "Write")

    def test_allowed_tools_from_yaml_list(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        _write(
            tmp_path / CONFIG_DIR / "commands" / "rev.md",
            "---\nallowed-tools:\n  - Bash\n  - Read\n---\nBody\n",
        )
        cmds = MarkdownCommand.discover(tmp_path)
        assert cmds["rev"].allowed_tools == ("Bash", "Read")

    def test_argument_hint_and_model_parsed(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        _write(
            tmp_path / CONFIG_DIR / "commands" / "rev.md",
            "---\nargument-hint: <path>\nmodel: opus\n---\nBody\n",
        )
        cmds = MarkdownCommand.discover(tmp_path)
        assert cmds["rev"].argument_hint == "<path>"
        assert cmds["rev"].model == "opus"

    def test_missing_commands_dir_is_no_op(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        assert MarkdownCommand.discover(tmp_path) == {}

    def test_the_group_root_sits_between_the_globals_and_the_project(self, tmp_path, monkeypatch):
        """The org's commands are a deliberate decision, so they beat
        a stray file in ``~`` — and a project knows more about itself
        than a group-wide command does."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        group = tmp_path / "group"
        group.mkdir()
        _write(home / CONFIG_DIR / "commands" / "shared.md", "GLOBAL\n")
        _write(group / "shared.md", "GROUP\n")
        _write(tmp_path / CONFIG_DIR / "commands" / "shared.md", "PROJECT\n")

        cmds = MarkdownCommand.discover(tmp_path, group_dir=group)

        assert "PROJECT" in cmds["shared"].body

    def test_the_group_wins_over_a_user_global(self, tmp_path, monkeypatch):
        """Same collision, one tier lower: the org's command beats
        the user's global, and the project's (absent here) would beat
        the org's."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        group = tmp_path / "group"
        group.mkdir()
        _write(home / CONFIG_DIR / "commands" / "shared.md", "GLOBAL\n")
        _write(group / "shared.md", "GROUP\n")

        cmds = MarkdownCommand.discover(tmp_path, group_dir=group)

        assert "GROUP" in cmds["shared"].body


class TestMarkdownCommandLoad:
    """``MarkdownCommand.load`` reads one file — the unit discovery
    is built from."""

    def test_the_name_is_the_stem(self, tmp_path):
        path = tmp_path / "review.md"
        path.write_text("Body.\n")

        cmd = MarkdownCommand.load(path)

        assert cmd is not None
        assert cmd.name == "review"
        assert cmd.path == path.resolve()

    def test_a_dotfile_is_not_a_command(self, tmp_path):
        path = tmp_path / ".backup.md"
        path.write_text("Body.\n")

        assert MarkdownCommand.load(path) is None

    def test_an_unreadable_file_is_not_a_command(self, tmp_path):
        """A path the process cannot read is skipped, not raised —
        discovery walks a directory the user does not own."""
        path = tmp_path / "locked.md"
        path.mkdir()

        assert MarkdownCommand.load(path) is None

    def test_a_non_utf8_file_is_not_a_command(self, tmp_path):
        path = tmp_path / "binary.md"
        path.write_bytes(b"\xff\xfe\x00broken")

        assert MarkdownCommand.load(path) is None

    def test_the_frontmatter_becomes_the_header(self, tmp_path):
        path = tmp_path / "review.md"
        path.write_text("---\ndescription: Look at it\n---\nBody.\n")

        cmd = MarkdownCommand.load(path)

        assert cmd is not None
        assert cmd.description == "Look at it"
        assert cmd.body == "Body.\n"


class TestMarkdownCommandModel:
    """Post-refactor MarkdownCommand is a Pydantic BaseModel with
    ``frozen=True``. These tests pin the invariants that callers
    (and any future ``model_dump`` serialisation) rely on."""

    def test_is_frozen(self, tmp_path):
        cmd = MarkdownCommand(name="x", path=tmp_path / "x.md", body="body")
        # Mutating a frozen BaseModel raises a ValidationError in v2;
        # we only care that assignment fails somehow.
        with pytest.raises(ValidationError):
            cmd.name = "y"  # type: ignore[misc]

    def test_defaults(self, tmp_path):
        cmd = MarkdownCommand(name="x", path=tmp_path / "x.md")
        assert cmd.description == ""
        assert cmd.allowed_tools == ()
        assert cmd.argument_hint == ""
        assert cmd.model is None
        assert cmd.body == ""

    def test_equality_is_by_value(self, tmp_path):
        # Two commands built from the same fields must compare equal —
        # otherwise the ``discover`` dict semantics (last-write-wins on
        # name collision) would depend on identity.
        a = MarkdownCommand(name="x", path=tmp_path / "x.md", body="B")
        b = MarkdownCommand(name="x", path=tmp_path / "x.md", body="B")
        assert a == b


# ── Token substitution ────────────────────────────────────────


class TestRenderArguments:
    @pytest.mark.asyncio
    async def test_arguments_substituted(self, tmp_path):
        cmd = MarkdownCommand(name="x", path=tmp_path / "x.md", body="Args: $ARGUMENTS done")
        assert await cmd.render("foo bar") == "Args: foo bar done"

    @pytest.mark.asyncio
    async def test_empty_arguments_substituted_empty(self, tmp_path):
        cmd = MarkdownCommand(name="x", path=tmp_path / "x.md", body="X[$ARGUMENTS]Y")
        assert await cmd.render("") == "X[]Y"

    @pytest.mark.asyncio
    async def test_no_arguments_token_is_no_op(self, tmp_path):
        cmd = MarkdownCommand(name="x", path=tmp_path / "x.md", body="Static body.")
        assert await cmd.render("ignored") == "Static body."


class TestRenderShell:
    @pytest.mark.asyncio
    async def test_shell_inline_captures_stdout(self, tmp_path):
        cmd = MarkdownCommand(name="x", path=tmp_path / "x.md", body="Echo: !`echo hello` done")
        out = await cmd.render("", project_dir=tmp_path)
        assert "Echo: hello done" in out

    @pytest.mark.asyncio
    async def test_shell_error_inlines_marker(self, tmp_path):
        cmd = MarkdownCommand(name="x", path=tmp_path / "x.md", body="!`false`")
        out = await cmd.render("", project_dir=tmp_path)
        assert "[error:" in out
        assert "exit 1" in out

    @pytest.mark.asyncio
    async def test_shell_command_not_found(self, tmp_path):
        cmd = MarkdownCommand(
            name="x", path=tmp_path / "x.md", body="!`thiscommandshouldnotexist_xyz`"
        )
        out = await cmd.render("", project_dir=tmp_path)
        assert "[error:" in out

    @pytest.mark.asyncio
    async def test_shell_concurrent_substitutions(self, tmp_path):
        """Multiple !`cmd` tokens fan out concurrently. The output
        is interleaved correctly because we map each match back to
        its captured index."""
        cmd = MarkdownCommand(
            name="x",
            path=tmp_path / "x.md",
            body="A=!`echo first` B=!`echo second`",
        )
        out = await cmd.render("", project_dir=tmp_path)
        assert "A=first" in out
        assert "B=second" in out

    @pytest.mark.asyncio
    async def test_a_command_with_no_project_runs_its_shell(self, tmp_path):
        """With no project there is no folder to be trusted, so there
        is nothing to gate."""
        cmd = MarkdownCommand(name="x", path=tmp_path / "x.md", body="Echo: !`echo hello`")
        out = await cmd.render("")
        assert "Echo: hello" in out

    @pytest.mark.asyncio
    async def test_a_command_from_outside_the_project_runs_its_shell(self, tmp_path):
        """A user-tier command is the user's own, not the project's —
        the trust gate is about the folder a command comes *from*."""
        user_cmd = tmp_path / "home" / CONFIG_DIR / "commands" / "go.md"
        user_cmd.parent.mkdir(parents=True)
        user_cmd.write_text("Echo: !`echo hello`\n")
        project = tmp_path / "project"
        project.mkdir()
        cmd = MarkdownCommand(name="go", path=user_cmd, body=user_cmd.read_text())

        out = await cmd.render("", project_dir=project)

        assert "Echo: hello" in out


class TestRenderFiles:
    @pytest.mark.asyncio
    async def test_at_path_inlines_file_contents(self, tmp_path):
        target = tmp_path / "data.txt"
        target.write_text("DATA-CONTENT")
        cmd = MarkdownCommand(name="x", path=tmp_path / "x.md", body="Read this: @./data.txt")
        out = await cmd.render("", project_dir=tmp_path)
        assert "DATA-CONTENT" in out

    @pytest.mark.asyncio
    async def test_at_path_trailing_punctuation_preserved(self, tmp_path):
        """A user writing ``See @README.md.`` shouldn't break the
        path lookup. We strip trailing ``.,;:`` for the file
        lookup and put it back in the output."""
        target = tmp_path / "README.md"
        target.write_text("README")
        cmd = MarkdownCommand(name="x", path=tmp_path / "x.md", body="See @./README.md.")
        out = await cmd.render("", project_dir=tmp_path)
        assert "README." in out

    @pytest.mark.asyncio
    async def test_missing_path_left_as_literal(self, tmp_path):
        cmd = MarkdownCommand(name="x", path=tmp_path / "x.md", body="See @./does-not-exist.md")
        out = await cmd.render("", project_dir=tmp_path)
        assert "@./does-not-exist.md" in out

    @pytest.mark.asyncio
    async def test_project_command_cannot_escape_project_dir(self, tmp_path):
        """SAFETY: a command file LIVING inside a project must not
        be able to read paths outside it. Defense in depth — the
        same property we enforce for rules-file @ imports."""
        # Create a project + an outside file
        project = tmp_path / "project"
        project.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("OUTSIDE-SECRET")
        cmd_file = project / CONFIG_DIR / "commands" / "leak.md"
        cmd_file.parent.mkdir(parents=True)
        cmd_file.write_text(f"Leak: @{outside.resolve()}")
        cmd = MarkdownCommand(name="leak", path=cmd_file, body=cmd_file.read_text())
        out = await cmd.render("", project_dir=project)
        # The literal token is preserved; the file contents are NOT inlined.
        assert "OUTSIDE-SECRET" not in out

    @pytest.mark.asyncio
    async def test_user_command_can_reference_anywhere(self, tmp_path):
        """A user-tier command (living in ``~/.igni/commands``)
        is explicitly authored by the user and CAN reference any
        path — they wrote it themselves. Only PROJECT commands are
        scoped."""
        home = tmp_path / "home"
        user_cmd_dir = home / CONFIG_DIR / "commands"
        user_cmd_dir.mkdir(parents=True)
        project = tmp_path / "project"
        project.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("OUTSIDE-CONTENT")
        cmd_file = user_cmd_dir / "ref.md"
        cmd_file.write_text(f"@{outside.resolve()}")
        cmd = MarkdownCommand(name="ref", path=cmd_file, body=cmd_file.read_text())
        out = await cmd.render("", project_dir=project)
        assert "OUTSIDE-CONTENT" in out


class TestCommandHandlerDispatch:
    """End-to-end: ``CommandHandler.handle`` should route an
    unknown built-in command to the markdown-command tier before
    falling through to the skill matcher."""

    @pytest.mark.asyncio
    async def test_command_handler_dispatches_to_markdown_command(self, tmp_path, monkeypatch):
        # Wire HOME so user-tier dirs land inside tmp_path.
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        (tmp_path / "home").mkdir()

        _write(
            tmp_path / CONFIG_DIR / "commands" / "review.md",
            "---\ndescription: Review\n---\nReview args: $ARGUMENTS\n",
        )

        session = MagicMock()
        session.project_dir = tmp_path
        session.settings.rules.cross_tool_support = True
        # The skill matcher would otherwise short-circuit and
        # return Unknown; force it to never match so we exercise
        # the markdown-command tier in isolation.
        session.skill_pool.match_user_command.return_value = None
        # ``_handle_skill`` calls ``stripped.split()[0]`` for the
        # error message — no need to mock anything else.

        handler = CommandHandler(session)
        result = await handler.handle("/review some thing")
        assert result.action == CommandAction.RUN_PROMPT
        assert "Review args: some thing" in result.content
        # No async-mock junk leaked through.
        assert isinstance(result.content, str)
        # Sanity: the mock wasn't accidentally awaited downstream.
        assert not isinstance(result.content, AsyncMock)

    @pytest.mark.asyncio
    async def test_builtin_command_still_beats_markdown(self, tmp_path, monkeypatch):
        """A markdown file at ``commands/help.md`` must NOT shadow
        the built-in ``/help`` — built-ins ship with the binary
        and a user shouldn't accidentally hijack them with a
        drop-in markdown file."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        (tmp_path / "home").mkdir()
        _write(
            tmp_path / CONFIG_DIR / "commands" / "help.md",
            "HIJACK\n",
        )

        session = MagicMock()
        session.project_dir = tmp_path
        session.settings.rules.cross_tool_support = True
        session.skill_pool.match_user_command.return_value = None

        handler = CommandHandler(session)
        result = await handler.handle("/help")
        # Built-in /help renders its own help text — the hijack
        # string must not appear.
        assert "HIJACK" not in (result.content or "")


class TestEndToEndRender:
    @pytest.mark.asyncio
    async def test_all_substitutions_compose(self, tmp_path):
        """All three token types in one body. Confirms the
        evaluation order: ``$ARGUMENTS`` → ``!`cmd``` → ``@path``
        (each substitution happens once, in that order)."""
        target = tmp_path / "ref.txt"
        target.write_text("REFERENCED")
        cmd = MarkdownCommand(
            name="x",
            path=tmp_path / "x.md",
            body="Args: $ARGUMENTS\nShell: !`echo SHELLED`\nFile: @./ref.txt",
        )
        out = await cmd.render("ARGSY", project_dir=tmp_path)
        assert "Args: ARGSY" in out
        assert "Shell: SHELLED" in out
        assert "File: REFERENCED" in out

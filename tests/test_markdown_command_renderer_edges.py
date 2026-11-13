"""The renderer's edges: what a shell token inlines or says when it goes
wrong, how the arguments are quoted, the untrusted-folder marker, and the
files an ``@path`` may and may not reach."""

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
        source=source, project_dir=project_dir, shell_allowed=shell_allowed
    )


class TestTheShellOutput:
    @pytest.mark.asyncio
    async def test_the_trailing_newlines_go_but_inner_ones_stay(self, tmp_path):
        out = await _renderer(tmp_path / "c.md").render("[!`printf 'a\\n\\nb\\n\\n\\n'`]", "")

        assert out == "[a\n\nb]"

    @pytest.mark.asyncio
    async def test_a_command_that_prints_nothing_inlines_nothing(self, tmp_path):
        assert await _renderer(tmp_path / "c.md").render("a!`true`b", "") == "ab"

    @pytest.mark.asyncio
    async def test_bytes_that_are_not_utf8_are_replaced_not_fatal(self, tmp_path):
        out = await _renderer(tmp_path / "c.md").render("!`printf 'a\\377b'`", "")

        assert out == "a�b"

    @pytest.mark.asyncio
    async def test_stderr_of_a_command_that_succeeds_is_not_inlined(self, tmp_path):
        out = await _renderer(tmp_path / "c.md").render("!`echo out; echo noise >&2`", "")

        assert out == "out"

    @pytest.mark.asyncio
    async def test_a_failure_says_the_exit_code_and_what_the_command_said(self, tmp_path):
        out = await _renderer(tmp_path / "c.md").render("!`echo bad thing >&2; exit 3`", "")

        assert out == "[error: exit 3 — bad thing]"

    @pytest.mark.asyncio
    async def test_a_failure_with_no_message_names_the_command(self, tmp_path):
        out = await _renderer(tmp_path / "c.md").render("!`exit 4`", "")

        assert out == "[error: exit 4 — 'exit 4']"

    @pytest.mark.asyncio
    async def test_a_timeout_says_how_long_and_which_command(self, tmp_path, monkeypatch):
        monkeypatch.setattr(renderer_module, "_SHELL_TIMEOUT_SECONDS", 0.05)

        out = await _renderer(tmp_path / "c.md").render("!`sleep 30`", "")

        assert out == "[error: timed out after 0.05s — 'sleep 30']"

    @pytest.mark.asyncio
    async def test_the_tokens_keep_their_places_when_one_fails(self, tmp_path):
        out = await _renderer(tmp_path / "c.md").render("1:!`echo a` 2:!`exit 1` 3:!`echo c`", "")

        assert out == "1:a 2:[error: exit 1 — 'exit 1'] 3:c"

    @pytest.mark.asyncio
    async def test_a_command_split_over_two_lines_is_not_a_token(self, tmp_path):
        body = "!`echo\nhi`"

        assert await _renderer(tmp_path / "c.md").render(body, "") == body


class TestTheArgumentsInAShellToken:
    @pytest.mark.asyncio
    async def test_no_arguments_are_an_empty_word(self, tmp_path):
        out = await _renderer(tmp_path / "c.md").render("!`printf '[%s]' $ARGUMENTS`", "")

        assert out == "[]"

    @pytest.mark.asyncio
    async def test_quotes_backticks_and_substitutions_arrive_as_text(self, tmp_path):
        hostile = 'it\'s `id` $(id) "q" $HOME ; rm -rf /'

        out = await _renderer(tmp_path / "c.md").render("!`printf '%s' $ARGUMENTS`", hostile)

        assert out == hostile

    @pytest.mark.asyncio
    async def test_every_mention_in_one_token_is_quoted(self, tmp_path):
        out = await _renderer(tmp_path / "c.md").render(
            "!`printf '%s|%s' $ARGUMENTS $ARGUMENTS`", "a b"
        )

        assert out == "a b|a b"

    @pytest.mark.asyncio
    async def test_the_arguments_outside_a_token_are_inserted_as_they_are(self, tmp_path):
        out = await _renderer(tmp_path / "c.md").render("$ARGUMENTS and $ARGUMENTS", "a b; c")

        assert out == "a b; c and a b; c"

    @pytest.mark.asyncio
    async def test_arguments_with_a_dollar_form_are_not_expanded_again(self, tmp_path):
        out = await _renderer(tmp_path / "c.md").render("$ARGUMENTS", "$ARGUMENTS")

        assert out == "$ARGUMENTS"


class TestTheUntrustedMarker:
    MARKER = "[shell command not run: this folder is not trusted — see `igni trust`]"

    @pytest.mark.asyncio
    async def test_each_token_becomes_the_marker_that_points_at_igni_trust(self, tmp_path):
        marker = tmp_path / "ran"
        out = await _renderer(tmp_path / "c.md", shell_allowed=False).render(
            f"a !`touch {marker}` b !`echo x` c", ""
        )

        assert out == f"a {self.MARKER} b {self.MARKER} c"
        assert not marker.exists()

    @pytest.mark.asyncio
    async def test_the_rest_of_the_body_still_renders(self, tmp_path):
        (tmp_path / "ref.txt").write_text("FILE")

        out = await _renderer(tmp_path / "c.md", shell_allowed=False).render(
            "!`x` @ref.txt $ARGUMENTS", "ARGS"
        )

        assert out == f"{self.MARKER} FILE ARGS"

    @pytest.mark.asyncio
    async def test_the_arguments_of_an_untrusted_command_stay_inert_text(self, tmp_path):
        marker = tmp_path / "ran"

        out = await _renderer(tmp_path / "c.md", shell_allowed=False).render(
            "$ARGUMENTS", f"!`touch {marker}`"
        )

        assert out == f"!`touch {marker}`"
        assert not marker.exists()


class TestTheFileTokens:
    @pytest.mark.asyncio
    async def test_an_address_in_prose_stays_as_written(self, tmp_path):
        body = "mail me@example.com or see [x](@nowhere)"

        assert await _renderer(tmp_path / "c.md").render(body, "") == body

    @pytest.mark.asyncio
    async def test_a_file_that_is_not_text_stays_a_literal(self, tmp_path):
        (tmp_path / "bin.dat").write_bytes(b"\xff\xfe\x00\x80")

        assert await _renderer(tmp_path / "c.md").render("@bin.dat", "") == "@bin.dat"

    @pytest.mark.asyncio
    async def test_a_symlink_in_the_project_to_a_file_outside_it_is_refused(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("OUTSIDE")
        (project / "link.txt").symlink_to(outside)

        out = await _renderer(project / "c.md", project_dir=project).render("@link.txt", "")

        assert out == "@link.txt"

    @pytest.mark.asyncio
    async def test_a_dotdot_path_out_of_the_project_is_refused(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (tmp_path / "outside.txt").write_text("OUTSIDE")

        out = await _renderer(project / "c.md", project_dir=project).render("@../outside.txt", "")

        assert out == "@../outside.txt"

    @pytest.mark.asyncio
    async def test_a_home_command_in_a_project_may_read_anywhere(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        home_cmds = tmp_path / "home"
        home_cmds.mkdir()
        (tmp_path / "outside.txt").write_text("OUTSIDE")

        out = await _renderer(home_cmds / "c.md", project_dir=project).render(
            f"@{tmp_path / 'outside.txt'}", ""
        )

        assert out == "OUTSIDE"

    @pytest.mark.asyncio
    async def test_an_inlined_file_does_not_run_the_shell_tokens_it_contains(self, tmp_path):
        marker = tmp_path / "ran"
        (tmp_path / "ref.txt").write_text(f"!`touch {marker}`")

        out = await _renderer(tmp_path / "c.md").render("@ref.txt", "")

        assert out == f"!`touch {marker}`"
        assert not marker.exists()

    @pytest.mark.asyncio
    async def test_an_inlined_file_keeps_its_own_trailing_newline_before_punctuation(
        self, tmp_path
    ):
        (tmp_path / "ref.txt").write_text("body\n")

        out = await _renderer(tmp_path / "c.md").render("see @ref.txt, then", "")

        assert out == "see body\n, then"

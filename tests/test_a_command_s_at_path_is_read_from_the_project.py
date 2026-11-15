"""A markdown command's relative ``@path`` is read from the project first.

CLAUDE_CODE_PARITY row 19 and ``markdown_commands.py`` promise the
project-relative reading of ``@path``. It was looked up only beside the
command file, in ``.igni/commands/``, so ``@facts.txt`` at the project root
stayed a literal (campaign R135). It is read from the project, and beside
the command file when the project has no such file.
"""

from __future__ import annotations

from pathlib import Path

from igni_core.core.utils.markdown_command_renderer import MarkdownCommandRenderer


def _renderer(project: Path) -> MarkdownCommandRenderer:
    commands = project / ".igni" / "commands"
    commands.mkdir(parents=True, exist_ok=True)
    return MarkdownCommandRenderer(
        source=commands / "zcode.md", project_dir=project, shell_allowed=False
    )


async def test_a_file_at_the_project_root_is_inlined(tmp_path: Path) -> None:
    (tmp_path / "facts.txt").write_text("PROJECT-FACT")

    text = await _renderer(tmp_path).render("Facts: @facts.txt", "")

    assert text == "Facts: PROJECT-FACT"


async def test_a_file_beside_the_command_still_is(tmp_path: Path) -> None:
    renderer = _renderer(tmp_path)
    (tmp_path / ".igni" / "commands" / "snippet.md").write_text("BESIDE")

    assert await renderer.render("@snippet.md", "") == "BESIDE"


async def test_the_project_s_file_wins_over_one_beside_the_command(tmp_path: Path) -> None:
    renderer = _renderer(tmp_path)
    (tmp_path / "x.txt").write_text("ROOT")
    (tmp_path / ".igni" / "commands" / "x.txt").write_text("BESIDE")

    assert await renderer.render("@x.txt", "") == "ROOT"

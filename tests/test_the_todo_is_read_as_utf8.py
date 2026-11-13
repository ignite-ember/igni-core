"""``.igni/TODO.md`` is read as UTF-8, whatever the machine's locale, and never stops a start.

The file is written by people and by other tools. A byte that is not
valid UTF-8 used to raise out of the instructions builder and so out of
the session start; it is now shown with the replacement character.
"""

from __future__ import annotations

from pathlib import Path

from igni_core.core.config.settings import Settings
from igni_core.core.paths import CONFIG_DIR
from igni_core.core.prompts.contributions import PromptContributions
from igni_core.core.prompts.fragments import PromptFragments
from igni_core.core.prompts.loader import PromptLoader
from igni_core.core.session.agent_builder.instructions_builder import InstructionsBuilder


def _todo_block(project: Path, data: bytes) -> str:
    path = project / CONFIG_DIR / "TODO.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    builder = InstructionsBuilder(
        project_dir=project,
        project_instructions="",
        workspace=None,
        settings=Settings(),
        output_styles={},
        active_output_style_name="",
        contributions=PromptContributions(),
        prompt_loader=PromptLoader(PromptFragments()),
    )
    out: list[str] = []
    builder.append_todo(out)
    [block] = out
    return block


def test_a_file_that_is_not_utf8_does_not_stop_the_session_starting(tmp_path):
    block = _todo_block(tmp_path, b"- caf\xe9 task\n")
    assert block == f"Active TODO ({CONFIG_DIR}/TODO.md):\n- caf� task"


def test_non_ascii_text_is_read_as_utf8(tmp_path):
    block = _todo_block(tmp_path, "- café ✓\n".encode())
    assert block.endswith("- café ✓")

"""What follows the base prompt in the main agent's instructions, and when.

Each block is added only when it has something to say, and always in the
same order: project instructions, the active TODO, the workspace context
(and each additional workspace's rules), the plan-mode block, the output
style, the memory writeback.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from igni_core.core.config.settings import Settings
from igni_core.core.paths import CONFIG_DIR
from igni_core.core.prompts.contribution_models import PromptOverride
from igni_core.core.prompts.contributions import PromptContributions
from igni_core.core.prompts.fragments import PromptFragments
from igni_core.core.prompts.loader import PromptLoader
from igni_core.core.session.agent_builder.instructions_builder import InstructionsBuilder
from igni_core.core.utils.context_memory import ProjectMemoryBank


class _Workspace:
    def __init__(self, context: str = "", dirs: list[Path] | None = None) -> None:
        self._context = context
        self.additional_dirs = dirs or []

    def get_context_instructions(self) -> str:
        return self._context


def _builder(
    project_dir: Path,
    *,
    instructions: str = "",
    workspace: _Workspace | None = None,
    styles: dict | None = None,
    active: str = "",
    settings: Settings | None = None,
    contributions: PromptContributions | None = None,
) -> InstructionsBuilder:
    return InstructionsBuilder(
        project_dir=project_dir,
        project_instructions=instructions,
        workspace=workspace or _Workspace(),
        settings=settings or Settings(),
        output_styles=styles or {},
        active_output_style_name=active,
        contributions=contributions or PromptContributions(),
        prompt_loader=PromptLoader(PromptFragments()),
    )


def _todo(project: Path, text: str | bytes | None) -> None:
    path = project / CONFIG_DIR / "TODO.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    elif text is not None:
        path.write_text(text, encoding="utf-8")


def _appended(method, builder: InstructionsBuilder) -> list[str]:
    out: list[str] = []
    getattr(builder, method)(out)
    return out


class TestProjectInstructions:
    def test_they_are_prefixed_with_their_heading(self, tmp_path):
        out = _appended("append_project_instructions", _builder(tmp_path, instructions="Use tabs."))
        assert out == ["Project instructions:\nUse tabs."]

    def test_none_adds_nothing(self, tmp_path):
        assert _appended("append_project_instructions", _builder(tmp_path)) == []


class TestTheActiveTodo:
    def test_the_file_is_shown_stripped_under_its_path(self, tmp_path):
        _todo(tmp_path, "\n  - ship it  \n\n")
        out = _appended("append_todo", _builder(tmp_path))
        assert out == [f"Active TODO ({CONFIG_DIR}/TODO.md):\n- ship it"]

    def test_no_file_adds_nothing(self, tmp_path):
        assert _appended("append_todo", _builder(tmp_path)) == []

    def test_a_blank_file_adds_nothing(self, tmp_path):
        _todo(tmp_path, " \n\t\n")
        assert _appended("append_todo", _builder(tmp_path)) == []

    def test_a_directory_of_that_name_adds_nothing(self, tmp_path):
        (tmp_path / CONFIG_DIR / "TODO.md").mkdir(parents=True)
        assert _appended("append_todo", _builder(tmp_path)) == []


class TestWorkspaceContext:
    def _extra(self, tmp_path: Path, name: str, rule: str | None) -> Path:
        extra = tmp_path / name
        extra.mkdir()
        if rule is not None:
            (extra / "igni.md").write_text(rule, encoding="utf-8")
        return extra

    def test_no_context_skips_the_additional_directories_too(self, tmp_path):
        extra = self._extra(tmp_path, "other", "Other rule")
        builder = _builder(tmp_path, workspace=_Workspace("", [extra]))
        assert _appended("append_workspace_context", builder) == []

    def test_the_context_comes_first_then_each_directory_with_rules(self, tmp_path):
        with_rules = self._extra(tmp_path, "svc-é", "Service rule")
        without = self._extra(tmp_path, "empty", None)
        builder = _builder(tmp_path, workspace=_Workspace("CTX", [with_rules, without]))
        out = _appended("append_workspace_context", builder)
        assert out[0] == "CTX"
        assert len(out) == 2
        assert out[1].startswith("Additional workspace (svc-é):\n")
        assert "Service rule" in out[1]

    def test_the_claude_md_of_a_directory_is_read_only_with_cross_tool_support(self, tmp_path):
        extra = self._extra(tmp_path, "other", None)
        (extra / "CLAUDE.md").write_text("Claude-only rule", encoding="utf-8")
        on, off = Settings(), Settings()
        on.rules.cross_tool_support = True
        off.rules.cross_tool_support = False
        workspace = _Workspace("CTX", [extra])
        assert "Claude-only rule" in "".join(
            _appended(
                "append_workspace_context", _builder(tmp_path, workspace=workspace, settings=on)
            )
        )
        assert "Claude-only rule" not in "".join(
            _appended(
                "append_workspace_context", _builder(tmp_path, workspace=workspace, settings=off)
            )
        )


class TestOutputStyle:
    @staticmethod
    def _styles(**bodies: str) -> dict:
        return {name: SimpleNamespace(name=name, body=body) for name, body in bodies.items()}

    def test_the_active_style_with_a_body_is_added(self, tmp_path):
        builder = _builder(tmp_path, styles=self._styles(terse="Be terse."), active="terse")
        assert _appended("append_output_style", builder) == ["# Output style: terse\n\nBe terse."]

    def test_an_unknown_style_adds_nothing(self, tmp_path):
        builder = _builder(tmp_path, styles=self._styles(terse="Be terse."), active="loud")
        assert _appended("append_output_style", builder) == []

    def test_a_style_without_a_body_adds_nothing(self, tmp_path):
        builder = _builder(tmp_path, styles=self._styles(plain=""), active="plain")
        assert _appended("append_output_style", builder) == []

    def test_an_empty_name_adds_nothing(self, tmp_path):
        builder = _builder(tmp_path, styles=self._styles(terse="Be terse."), active="")
        assert _appended("append_output_style", builder) == []


class TestTheFixedBlocks:
    def test_the_memory_writeback_is_always_added(self, tmp_path):
        out = _appended("append_memory_writeback", _builder(tmp_path))
        assert out == [ProjectMemoryBank(tmp_path).writeback_instructions()]

    def test_the_plan_mode_block_is_always_added(self, tmp_path):
        [block] = _appended("append_plan_mode_nudge", _builder(tmp_path))
        assert block.startswith("PLAN MODE")

    def test_an_extension_can_replace_the_plan_mode_block(self, tmp_path):
        contributions = PromptContributions()
        contributions.set_owner(
            "a.ext", "Ay", overrides=[PromptOverride(target="plan_mode", text="OUR PLAN MODE")]
        )
        out = _appended("append_plan_mode_nudge", _builder(tmp_path, contributions=contributions))
        assert out == ["OUR PLAN MODE"]


class TestAssemble:
    def test_the_blocks_follow_the_prompt_in_order(self, tmp_path):
        _todo(tmp_path, "- ship")
        builder = _builder(
            tmp_path,
            instructions="Use tabs.",
            workspace=_Workspace("CTX"),
            styles={"s": SimpleNamespace(name="s", body="B")},
            active="s",
        )
        out = builder.assemble("BASE")
        assert out[0] == "BASE"
        assert out[1] == "Project instructions:\nUse tabs."
        assert out[2].startswith("Active TODO")
        assert out[3] == "CTX"
        assert out[4].startswith("PLAN MODE")
        assert out[5] == "# Output style: s\n\nB"
        assert out[6] == ProjectMemoryBank(tmp_path).writeback_instructions()
        assert len(out) == 7

    def test_with_nothing_to_say_only_the_fixed_blocks_follow(self, tmp_path):
        out = _builder(tmp_path).assemble("BASE")
        assert len(out) == 3
        assert out[0] == "BASE" and out[1].startswith("PLAN MODE")

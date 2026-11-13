"""The main prompt's catalogue, its skills block and its capability spans.

``{{AGENT_CATALOG}}`` is filled from the agent pool (with a stated
fallback when nothing is loaded); the skills block is appended only when
there are skills and auto-trigger is on, and tells the agent it may invoke
them itself; the user-memory spans keep one branch, drop the other, leave
no marker behind and no run of blank lines where a block was cut.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from igni_core.core.prompts.contributions import PromptContributions
from igni_core.core.prompts.fragments import PromptFragments
from igni_core.core.prompts.loader import PromptLoader
from igni_core.core.session.agent_builder.prompt_builder import PromptBuilder


def _builder(
    *,
    catalog: str = "- researcher: reads things",
    skills: str = "",
    auto_trigger: bool = True,
    user_memory: bool = True,
    loader: PromptLoader | None = None,
) -> PromptBuilder:
    return PromptBuilder(
        project_dir=Path("/nonexistent"),
        settings=SimpleNamespace(skills=SimpleNamespace(auto_trigger=auto_trigger)),
        agent_catalog=catalog,
        skill_descriptions=skills,
        prompt_loader=loader or PromptLoader(PromptFragments()),
        contributions=PromptContributions(),
        user_memory_available=user_memory,
    )


class TestTheAgentCatalogue:
    def test_every_placeholder_is_filled_from_the_catalogue(self):
        out = _builder().substitute_placeholders("a {{AGENT_CATALOG}} b {{AGENT_CATALOG}}")
        assert out == "a - researcher: reads things b - researcher: reads things"

    def test_an_empty_catalogue_says_no_agents_are_loaded(self):
        out = _builder(catalog="").substitute_placeholders("[{{AGENT_CATALOG}}]")
        assert out == "[(no agents loaded)]"

    def test_the_rendered_prompt_carries_the_catalogue(self):
        assert "- researcher: reads things" in _builder().render()
        assert "{{AGENT_CATALOG}}" not in _builder().render()


class TestTheSkillsBlock:
    def test_it_is_appended_with_what_the_agent_may_do(self):
        out = _builder(skills="- /ship: ships it").append_skills("PROMPT")
        assert out.startswith("PROMPT\n\n## Available Skills\n\n")
        assert "**You can invoke these yourself** with the `slash_command` tool" in out
        assert '`slash_command("/commit")`' in out
        assert "**Prefer a skill over improvising.**" in out
        assert out.endswith("so and continue.\n- /ship: ships it")

    def test_no_descriptions_leave_the_prompt_as_it_was(self):
        assert _builder(skills="").append_skills("PROMPT") == "PROMPT"

    def test_auto_trigger_off_leaves_the_prompt_as_it_was(self):
        assert _builder(skills="- /ship", auto_trigger=False).append_skills("PROMPT") == "PROMPT"

    def test_the_skills_come_last_in_the_rendered_prompt(self):
        assert _builder(skills="- /ship: ships it").render().endswith("- /ship: ships it")

    def test_no_skills_block_in_the_rendered_prompt_without_descriptions(self):
        assert "## Available Skills" not in _builder().render()


class TestTheSpans:
    def test_the_kept_branch_survives_the_dropped_one_is_cut_and_no_marker_stays(self):
        text = (
            "head\n\n<!-- user-memory:only -->\nwith\n<!-- /user-memory:only -->\n"
            "<!-- user-memory:absent -->\nwithout\n<!-- /user-memory:absent -->\n\ntail"
        )
        assert _builder(user_memory=True).resolve_conditionals(text) == "head\n\nwith\n\ntail"
        assert _builder(user_memory=False).resolve_conditionals(text) == "head\n\nwithout\n\ntail"

    def test_a_cut_block_leaves_no_run_of_blank_lines(self):
        text = "a\n\n\n<!-- user-memory:absent -->\nx\n<!-- /user-memory:absent -->\n\n\n\nb"
        assert _builder(user_memory=True).resolve_conditionals(text) == "a\n\nb"

    def test_a_span_may_cover_many_lines(self):
        text = "a<!-- user-memory:only -->\nl1\n\nl2\n<!-- /user-memory:only -->b"
        assert _builder(user_memory=False).resolve_conditionals(text) == "ab"

    @pytest.mark.parametrize("user_memory", [True, False])
    @pytest.mark.parametrize("branch", ["only", "absent"])
    def test_an_unbalanced_marker_is_refused_in_the_kept_and_the_dropped_branch(
        self, user_memory, branch
    ):
        text = f"x <!-- user-memory:{branch} --> y"
        with pytest.raises(ValueError, match=f"unbalanced user-memory:{branch}"):
            _builder(user_memory=user_memory).resolve_conditionals(text)

    def test_a_close_marker_without_its_open_is_refused(self):
        with pytest.raises(ValueError, match=r"0 open, 1 close"):
            _builder().resolve_conditionals("x <!-- /user-memory:only --> y")

    def test_text_without_markers_is_only_stripped(self):
        assert _builder().resolve_conditionals("\n plain \n") == "plain"


class TestTheSectionFile:
    def test_an_unreadable_section_is_omitted_with_a_warning(self, caplog, tmp_path):
        (tmp_path / "persisting.md").mkdir()  # reading a directory raises OSError
        builder = _builder(loader=PromptLoader(PromptFragments(), tmp_path))
        with caplog.at_level("WARNING"):
            assert builder.include_sections("a{{PERSISTING}}b") == "ab"
        assert "prompt section 'persisting' missing" in caplog.text


class TestOnlyUserMemoryIsAConditional:
    def test_a_knowledge_store_marker_is_not_resolved_by_igni(self):
        """What an extension adds is stated by editing the prompt, not by a span."""
        text = "a <!-- knowledge-store:only -->kept<!-- /knowledge-store:only --> b"
        assert _builder().resolve_conditionals(text) == text

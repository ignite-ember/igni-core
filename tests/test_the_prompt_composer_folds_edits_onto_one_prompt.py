"""How one target's override and edits are folded into one prompt.

Pins the composer through the table that feeds it: what the starting
point is, which edits land, which are skipped and why, and who owns the
agent's frontmatter afterwards.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from igni_core.core.prompts.composition import PromptComposer
from igni_core.core.prompts.contribution_models import EditOp, PromptEdit, PromptOverride
from igni_core.core.prompts.contributions import PromptContributions
from igni_core.core.prompts.document import Position

SECTION = "<!-- section:s -->\nbody\n<!-- /section:s -->"
AGENT = "---\nname: x\n---\n" + SECTION


def _edit(target: str, op: EditOp, **fields) -> PromptEdit:
    return PromptEdit(target=target, op=op, text=fields.pop("text", "t"), **fields)


def test_nothing_to_start_from_is_no_prompt():
    assert PromptContributions().compose("agent:ghost", None) is None


def test_an_extension_only_agent_starts_from_its_override_and_records_the_file():
    table = PromptContributions()
    path = Path("/ext/agents/ghost.md")
    table.set_owner(
        "a.ext", "Ay", overrides=[PromptOverride(target="agent:ghost", text=AGENT, path=path)]
    )
    composition = table.compose("agent:ghost", None)
    assert composition is not None
    assert composition.override_owner == "a.ext"
    assert composition.override_path == path
    assert composition.frontmatter_owner == "a.ext"
    assert composition.skipped == []


def test_igni_s_own_agent_has_no_frontmatter_owner_and_a_main_prompt_never_has_one():
    table = PromptContributions()
    agent = table.compose("agent:x", AGENT)
    main = table.compose("main", SECTION)
    assert agent is not None and main is not None
    assert agent.frontmatter_owner is None and agent.override_owner is None
    assert agent.override_path is None
    assert main.frontmatter_owner is None


def test_replacing_the_frontmatter_hands_it_to_the_replacer():
    table = PromptContributions()
    table.set_owner(
        "a.ext",
        "Ay",
        edits=[_edit("agent:x", EditOp.REPLACE, section="frontmatter", text="---\nname: y\n---")],
    )
    composition = table.compose("agent:x", AGENT)
    assert composition is not None
    assert composition.frontmatter_owner == "a.ext"
    assert composition.document.render().startswith("---\nname: y\n---")


def test_a_frontmatter_replace_on_a_plain_prompt_is_skipped_not_raised():
    table = PromptContributions()
    table.set_owner(
        "a.ext", "Ay", edits=[_edit("main", EditOp.REPLACE, section="frontmatter", text="x")]
    )
    composition = table.compose("main", SECTION)
    assert composition is not None
    [line] = composition.skipped
    assert line == "a.ext: replacing main#frontmatter — it has no section 'frontmatter'"


def test_a_refused_edit_is_skipped_with_its_owner_and_reason_and_the_rest_still_land():
    table = PromptContributions()
    table.set_owner(
        "a.ext",
        "Ay",
        edits=[
            _edit("agent:x", EditOp.REPLACE, section="frontmatter", text="no fences"),
            _edit("agent:x", EditOp.REPLACE, section="s", text="<!-- section:open -->"),
            _edit("agent:x", EditOp.APPEND, name="tail", text="the tail"),
        ],
    )
    composition = table.compose("agent:x", AGENT)
    assert composition is not None
    first, second = composition.skipped
    assert first == (
        "a.ext: replacing agent:x#frontmatter — "
        "an agent's frontmatter is a YAML block between '---' lines"
    )
    assert second.startswith("a.ext: replacing agent:x#s — ")
    assert "opens inside" in second
    assert "the tail" in composition.document.render()
    assert "body" in composition.document.render()


def test_an_insert_before_the_frontmatter_is_skipped():
    table = PromptContributions()
    table.set_owner(
        "a.ext",
        "Ay",
        edits=[
            _edit(
                "agent:x",
                EditOp.INSERT,
                section="frontmatter",
                position=Position.BEFORE,
                name="n",
                text="x",
            )
        ],
    )
    composition = table.compose("agent:x", AGENT)
    assert composition is not None
    [line] = composition.skipped
    assert line == (
        "a.ext: inserting n before agent:x#frontmatter — nothing goes before an agent's frontmatter"
    )


def test_an_insert_with_no_position_lands_at_the_end_like_an_append():
    table = PromptContributions()
    table.set_owner(
        "a.ext", "Ay", edits=[_edit("main", EditOp.INSERT, section="s", name="n", text="NEW")]
    )
    composition = table.compose("main", SECTION)
    assert composition is not None
    assert composition.document.render().endswith("NEW")
    assert composition.skipped == []


def test_the_exclusive_owner_s_own_edits_still_apply_and_others_say_so():
    table = PromptContributions()
    table.set_owner(
        "a.ext",
        "Ay",
        overrides=[PromptOverride(target="main", text=SECTION, exclusive=True)],
        edits=[_edit("main", EditOp.APPEND, name="mine", text="MINE")],
    )
    table.set_owner("b.ext", "Bee", edits=[_edit("main", EditOp.APPEND, name="x", text="THEIRS")])
    composition = table.compose("main", "igni")
    assert composition is not None
    rendered = composition.document.render()
    assert "MINE" in rendered and "THEIRS" not in rendered
    assert composition.skipped == [
        "b.ext: appending x to main — a.ext overrides it exclusively",
    ]


def test_the_skipped_lines_are_logged_at_info(caplog):
    table = PromptContributions()
    table.set_owner("a.ext", "Ay", edits=[_edit("main", EditOp.REPLACE, section="gone", text="x")])
    with caplog.at_level(logging.INFO, logger="igni_core.core.prompts.composition"):
        table.compose("main", SECTION)
    [record] = [r for r in caplog.records if r.name == "igni_core.core.prompts.composition"]
    assert record.levelno == logging.INFO
    assert "prompt main: a.ext: replacing main#gone — it has no section 'gone'" in record.message


def test_a_lost_replace_is_a_warning_and_not_a_skipped_line(caplog):
    table = PromptContributions()
    table.set_owner("a.ext", "Ay", edits=[_edit("main", EditOp.REPLACE, section="s", text="A")])
    table.set_owner("b.ext", "Bee", edits=[_edit("main", EditOp.REPLACE, section="s", text="B")])
    with caplog.at_level(logging.WARNING):
        composition = table.compose("main", SECTION)
    assert composition is not None
    assert composition.document.render() == "A"
    assert composition.skipped == []
    assert "a.ext and b.ext both replace main#s; a.ext keeps it" in caplog.text


def test_unbalanced_markers_in_the_starting_text_are_an_error_not_a_skip():
    with pytest.raises(ValueError, match="never closed"):
        PromptContributions().compose("main", "<!-- section:s -->\nno end")


def test_prepare_runs_on_the_starting_text_before_it_is_read_as_sections():
    table = PromptContributions()
    table.set_owner("a.ext", "Ay", edits=[_edit("main", EditOp.REPLACE, section="in", text="X")])
    composition = table.compose(
        "main",
        "{{INC}}",
        prepare=lambda t: t.replace("{{INC}}", "<!-- section:in -->\n.\n<!-- /section:in -->"),
    )
    assert composition is not None
    assert composition.document.render() == "X"


def test_a_prepare_that_raises_propagates():
    def boom(_: str) -> str:
        raise RuntimeError("no")

    with pytest.raises(RuntimeError):
        PromptContributions().compose("main", "x", prepare=boom)


def test_an_unspellable_target_is_refused():
    with pytest.raises(ValueError, match="is not a prompt target"):
        PromptComposer(target="Main", base="x", override=None, edits=[]).compose()


def test_no_edits_gives_the_text_unchanged():
    composition = PromptComposer(target="main", base="plain", override=None, edits=[]).compose()
    assert composition is not None
    assert composition.document.render() == "plain"

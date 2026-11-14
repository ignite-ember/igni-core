"""What an extension contributes to a prompt, as data.

An override and an edit are frozen once made; an edit describes itself
in one line, which is what the log and the skipped report show.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from igni_core.core.prompts.contribution_models import (
    Composition,
    EditOp,
    PromptEdit,
    PromptOverride,
)
from igni_core.core.prompts.document import Position, PromptDocument


def test_the_edit_operations_are_spelled_as_the_manifests_spell_them():
    assert [op.value for op in EditOp] == ["append", "insert", "replace"]
    assert PromptEdit(target="main", op="replace", section="s", text="t").op is EditOp.REPLACE
    with pytest.raises(ValidationError):
        PromptEdit(target="main", op="delete", text="t")


def test_an_override_defaults_to_shared_unconditional_and_pathless():
    override = PromptOverride(target="main", text="t")
    assert override.exclusive is False
    assert override.when is None
    assert override.path is None


def test_an_override_path_given_as_text_becomes_a_path():
    assert PromptOverride(target="main", text="t", path="/a/b.md").path == Path("/a/b.md")  # type: ignore[arg-type]


def test_an_override_and_an_edit_cannot_be_changed_once_made():
    override = PromptOverride(target="main", text="t")
    edit = PromptEdit(target="main", op=EditOp.APPEND, text="t")
    with pytest.raises(ValidationError):
        override.text = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        edit.text = "other"  # type: ignore[misc]


def test_a_replace_describes_itself_with_its_target_and_section():
    edit = PromptEdit(target="agent:x", op=EditOp.REPLACE, section="s", text="t")
    assert edit.describe() == "replacing agent:x#s"


def test_an_insert_describes_itself_with_the_name_and_where_it_goes():
    after = PromptEdit(
        target="main", op=EditOp.INSERT, section="s", position=Position.AFTER, name="n", text="t"
    )
    before = after.model_copy(update={"position": Position.BEFORE})
    assert after.describe() == "inserting n after main#s"
    assert before.describe() == "inserting n before main#s"


def test_an_append_and_an_insert_without_a_position_describe_as_an_append():
    assert (
        PromptEdit(target="plan_mode", op=EditOp.APPEND, name="n", text="t").describe()
        == "appending n to plan_mode"
    )
    assert (
        PromptEdit(target="main", op=EditOp.INSERT, section="s", name="n", text="t").describe()
        == "appending n to main"
    )


def test_every_composition_has_a_skipped_list_of_its_own():
    first = Composition(document=PromptDocument("a"))
    second = Composition(document=PromptDocument("b"))
    first.skipped.append("one")
    assert second.skipped == []
    assert first.override_owner is None
    assert first.override_path is None
    assert first.frontmatter_owner is None

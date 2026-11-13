"""The parts of a prompt document the other tests leave open.

Insert beside an agent's frontmatter, the refusals and their wording,
immutability, and the targets igni says exist.
"""

from __future__ import annotations

import pytest

from igni_core.core.prompts.document import (
    EditRefused,
    Position,
    PromptDocument,
    PromptKind,
    PromptTarget,
    PromptTargets,
)

S = "<!-- section:s -->\nbody\n<!-- /section:s -->"
AGENT = "---\nname: x\n---\n" + S


def test_an_insert_after_the_frontmatter_lands_after_it_and_keeps_the_order_made():
    document = PromptDocument(AGENT, agent=True)
    document = document.insert("frontmatter", Position.AFTER, "one", "first")
    document = document.insert("frontmatter", Position.AFTER, "two", "second")
    rendered = document.render()
    assert rendered.startswith("---\nname: x\n---\n")
    assert rendered.index("first") < rendered.index("second") < rendered.index("body")


def test_nothing_goes_before_the_frontmatter():
    with pytest.raises(EditRefused, match="nothing goes before an agent's frontmatter"):
        PromptDocument(AGENT, agent=True).insert("frontmatter", Position.BEFORE, "n", "t")


def test_a_frontmatter_without_its_fences_is_refused_and_one_with_whitespace_is_trimmed():
    document = PromptDocument(AGENT, agent=True)
    with pytest.raises(EditRefused, match="YAML block between '---' lines"):
        document.replace("frontmatter", "name: y")
    with pytest.raises(EditRefused):
        document.replace("frontmatter", "---\nname: y\n")
    replaced = document.replace("frontmatter", "\n  ---\nname: y\n---  \n\n")
    assert replaced.render().startswith("---\nname: y\n---")


def test_an_unknown_section_is_refused_by_name_for_replace_and_insert():
    document = PromptDocument(S)
    with pytest.raises(EditRefused, match="there is no section 'gone'"):
        document.replace("gone", "t")
    with pytest.raises(EditRefused, match="there is no section 'gone'"):
        document.insert("gone", Position.AFTER, "n", "t")


def test_a_replacement_that_would_nest_a_section_is_refused():
    with pytest.raises(ValueError, match="opens inside"):
        PromptDocument(S).replace("s", "<!-- section:inner -->")


def test_every_edit_returns_a_new_document_and_the_original_is_untouched():
    original = PromptDocument(AGENT, agent=True)
    before = original.text
    original.replace("s", "new")
    original.replace("frontmatter", "---\nname: y\n---")
    original.insert("s", Position.BEFORE, "n", "t")
    original.append("n", "t")
    assert original.text == before


def test_an_empty_text_and_an_agent_with_no_header_have_no_sections():
    assert PromptDocument("").sections() == []
    plain = PromptDocument(S, agent=True)
    assert plain.frontmatter is None and plain.sections() == ["s"]
    assert PromptDocument("").render() == ""


def test_added_sections_are_never_listed_and_three_blank_lines_collapse():
    document = PromptDocument(S).append("extra", "more")
    assert document.sections() == ["s"]
    assert PromptDocument.unmarked("a\n\n\n\n\nb\n") == "a\n\nb"


def test_a_target_names_its_document_and_whether_it_is_whole():
    whole = PromptTarget.parse("agent:acme.notes:archivist")
    assert whole.kind is PromptKind.AGENT and whole.agent == "acme.notes:archivist"
    assert whole.whole is True and whole.document == "agent:acme.notes:archivist"
    part = PromptTarget.parse("main#workflow")
    assert part.whole is False and part.document == "main" and str(part) == "main#workflow"


@pytest.mark.parametrize("spelled", ["", "agent:", "Main", "main#", "main#Up", "agent:Bad", "x"])
def test_what_is_not_a_target_is_refused(spelled):
    with pytest.raises(ValueError, match="is not a prompt target"):
        PromptTarget.parse(spelled)


def test_the_targets_say_which_documents_and_sections_exist():
    targets = PromptTargets.of(
        {"main": PromptDocument(S), "agent:x": PromptDocument(AGENT, agent=True)}
    )
    assert targets.documents == ["main", "agent:x"]
    assert targets.sections == ["main#s", "agent:x#frontmatter", "agent:x#s"]
    assert targets.has(PromptTarget.parse("main")) is True
    assert targets.has(PromptTarget.parse("main#s")) is True
    assert targets.has(PromptTarget.parse("main#t")) is False
    assert targets.has(PromptTarget.parse("agent:y")) is False
    assert targets.has(PromptTarget.parse("agent:x#frontmatter")) is True

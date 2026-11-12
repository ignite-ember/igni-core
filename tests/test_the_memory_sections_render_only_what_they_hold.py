"""``/memory``'s sections, one by one: what each renders, what it drops.

The whole-command rendering is pinned in ``test_memory_shows_what_was_learned.py``;
here is each section at its edges, and the recall's choices of section.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from igni_core.backend.schemas_memory import (
    EntityEntry,
    EntityMemorySection,
    FallbackSection,
    LearningRecall,
    SessionContextSection,
    UserMemorySection,
    UserProfileSection,
)
from igni_core.protocol.schemas.enums import CommandResultKind


def test_an_entity_without_a_name_is_listed_under_a_question_mark() -> None:
    assert EntityEntry(name=None, description="x").to_line() == "- **?**: x\n"
    assert EntityEntry(name="igni", description=None).to_line() == "- **igni**: \n"


def test_the_entity_directory_is_read_off_the_stores_attributes() -> None:
    store = {"directory": [SimpleNamespace(name="igni", description="CLI")], "entities": []}

    section = EntityMemorySection.from_store(store)

    assert section.render() == "## Entity Memory\n- **igni**: CLI\n"


def test_a_recall_whose_directory_is_empty_renders_no_section() -> None:
    assert EntityMemorySection.from_store({"directory": [], "entities": [1]}).render() == ""


def test_a_profile_lists_only_the_attributes_that_are_set_with_spaced_titles() -> None:
    profile = SimpleNamespace(preferred_name="Ada", expertise="compilers")

    assert UserProfileSection.from_store(profile).render() == (
        "## User Profile\n- **Preferred Name**: Ada\n- **Expertise**: compilers\n"
    )


def test_a_profile_with_a_single_field_lists_that_one() -> None:
    assert UserProfileSection.from_store(SimpleNamespace(role="lead")).render() == (
        "## User Profile\n- **Role**: lead\n"
    )


def test_a_profile_holding_nothing_renders_nothing() -> None:
    assert UserProfileSection.from_store(SimpleNamespace(name="", role=None)).render() == ""


def test_memories_without_content_are_dropped_and_unicode_survives() -> None:
    memories = SimpleNamespace(memories=[{"content": "likes čaj"}, {"content": ""}, {}])

    assert UserMemorySection.from_store(memories).render() == "## User Memory\n- likes čaj\n"


def test_a_session_context_without_a_summary_renders_nothing() -> None:
    assert SessionContextSection.from_store(SimpleNamespace(summary=None)).render() == ""
    assert SessionContextSection.from_store(SimpleNamespace(summary="Doing X")).render() == (
        "## Session Context\nDoing X\n"
    )


def test_an_unknown_store_is_titled_from_its_name_and_shows_its_str() -> None:
    section = FallbackSection.from_store("decision_log", ["a", "b"])

    assert section.render() == "## Decision Log\n['a', 'b']\n"


def test_the_recall_keeps_the_store_order_and_skips_falsy_stores() -> None:
    recall = LearningRecall.from_stores(
        {
            "zeta_notes": "z",
            "user_profile": None,
            "session_context": SimpleNamespace(summary="S"),
            "alpha_notes": "",
            "user_memory": SimpleNamespace(memories=[{"content": "m"}]),
        }
    )

    assert [type(s) for s in recall.sections] == [
        FallbackSection,
        SessionContextSection,
        UserMemorySection,
    ]
    assert recall.to_command_result().content == (
        "## Zeta Notes\nz\n\n\n## Session Context\nS\n\n\n## User Memory\n- m\n"
    )


@pytest.mark.parametrize("stores", [{}, {"user_profile": None, "user_memory": []}])
def test_nothing_to_show_says_nothing_is_stored_yet(stores: dict[str, object]) -> None:
    result = LearningRecall.from_stores(stores).to_command_result()

    assert result.kind is CommandResultKind.INFO
    assert result.content == LearningRecall.EMPTY_MESSAGE


def test_sections_that_render_empty_do_not_count_as_something_to_show() -> None:
    recall = LearningRecall.from_stores({"user_profile": SimpleNamespace(name="")})

    assert recall.to_command_result().content == LearningRecall.EMPTY_MESSAGE

"""The learning boot's failure path and the user-memory block it renders.

``test_learning.py`` pins the boot's other paths; the layout of the block
with no memories is pinned in ``test_the_system_prompt_keeps_its_trained_layout.py``.
Here: a constructor that raises is an envelope, never an exception; the
machine builds the trained store with the config Agno resolved; and the
block with memories, with memories only a ``memories`` list carries, and
with tools exposed or not.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from agno.db.in_memory import InMemoryDb
from agno.learn.config import UserMemoryConfig
from agno.learn.machine import LearningMode

import igni_core.core.learn as learn
from igni_core.core.config.settings import Settings
from igni_core.core.learn import TrainedUserMemoryStore, create_learning_machine


def _store(*, tools: bool) -> TrainedUserMemoryStore:
    return TrainedUserMemoryStore(
        config=UserMemoryConfig(
            mode=LearningMode.AGENTIC if tools else LearningMode.ALWAYS,
            enable_agent_tools=tools,
            agent_can_update_memories=tools,
        )
    )


def _enabled() -> Settings:
    settings = Settings()
    settings.learning.enabled = True
    return settings


def test_a_machine_that_cannot_be_built_is_a_reason_not_an_exception(monkeypatch):
    class _Broken:
        def __init__(self, **_):
            raise RuntimeError("store is locked")

    monkeypatch.setattr(learn, "TrainedLearningMachine", _Broken)
    result = create_learning_machine(_enabled(), InMemoryDb())
    assert result.ok is False
    assert result.machine is None
    assert result.reason == "construction failed: store is locked"


def test_disabled_wins_over_a_missing_database():
    settings = Settings()
    settings.learning.enabled = False
    assert create_learning_machine(settings, None).reason == "disabled in settings"


def test_the_machine_builds_the_trained_store_with_the_config_agno_resolved():
    machine = create_learning_machine(_enabled(), InMemoryDb()).machine
    store = machine.user_memory_store
    assert isinstance(store, TrainedUserMemoryStore)
    assert store.config.db is machine.db
    assert store.config.mode == LearningMode.AGENTIC


def test_no_memories_and_no_tools_is_no_block_at_all():
    store = _store(tools=False)
    assert store.build_context(None) == ""
    assert store.build_context(SimpleNamespace(get_memories_text=lambda: "")) == ""
    assert store.build_context(SimpleNamespace(memories=[])) == ""


def test_no_memories_with_tools_says_so_and_names_the_tool():
    block = _store(tools=True).build_context(SimpleNamespace(memories=[]))
    assert block.startswith("<user_memory>\nNo memories saved about this user yet.")
    assert "update_user_memory" in block
    assert block.endswith("</user_memory>")


def test_memories_are_followed_by_the_guidelines_inside_the_block():
    block = _store(tools=False).build_context(
        SimpleNamespace(get_memories_text=lambda: "- likes tabs")
    )
    assert block.startswith("<user_memory>\n- likes tabs\n")
    assert block.index("<memory_application_guidelines>") < block.index("</user_memory>")
    assert "<memory_updates>" not in block
    assert block.endswith("</memory_application_guidelines>\n</user_memory>")


def test_a_memories_list_is_rendered_by_content_or_by_its_own_text():
    block = _store(tools=False).build_context(
        SimpleNamespace(memories=[{"content": "likes tabs"}, {"note": "x"}])
    )
    assert "- likes tabs\n" in block
    assert "- {'note': 'x'}\n" in block


def test_exposed_tools_add_a_memory_updates_section_after_the_guidelines():
    block = _store(tools=True).build_context(
        SimpleNamespace(get_memories_text=lambda: "- likes tabs")
    )
    guidelines = block.index("</memory_application_guidelines>")
    updates = block.index("<memory_updates>")
    assert guidelines < updates < block.index("update_user_memory")
    assert block.endswith("</memory_updates>\n</user_memory>")


@pytest.mark.parametrize("tools", [True, False])
def test_the_store_leaves_its_guidance_to_the_block(tools):
    assert _store(tools=tools).instructions() == ""

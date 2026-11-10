"""``/memory`` — what the Learning Machine remembers, or why it can't say.

Two ways this used to lie: a recall that raised was rendered as "No
learnings stored yet", and the entity store's recall (a mapping with a
``directory``) was probed for an ``entities`` attribute it never has, so
known entities never showed up.
"""

from __future__ import annotations

from types import SimpleNamespace

from agno.learn.schemas import EntityMemory, Memories, SessionContext, UserProfile

from igni_core.backend.cmd_memory import MemoryCommand
from igni_core.backend.schemas_memory import LearningRecall
from igni_core.protocol.schemas.enums import CommandResultKind


class _Learning:
    def __init__(self, stores: dict[str, object] | None = None, error: Exception | None = None):
        self._stores = stores or {}
        self._error = error

    async def arecall(self, user_id: str) -> dict[str, object]:
        if self._error is not None:
            raise self._error
        return self._stores


def _command(learning: _Learning | None) -> MemoryCommand:
    return MemoryCommand(SimpleNamespace(learning_machine=learning, user_id="u1"))


async def test_every_known_store_renders_its_section() -> None:
    stores = {
        "user_profile": UserProfile(user_id="u1", name="Ada", preferred_name="A"),
        "user_memory": Memories(user_id="u1", memories=[{"content": "likes tea"}, {"content": ""}]),
        "entity_memory": {
            "directory": [
                EntityMemory(entity_id="e1", entity_type="project", name="igni", description="CLI")
            ],
            "entities": [],
            "related_names": [],
        },
        "session_context": SessionContext(session_id="s1", summary="Refactoring"),
        "decision_log": "two decisions",
    }

    result = await _command(_Learning(stores)).show()

    assert result.content == (
        "## User Profile\n- **Name**: Ada\n- **Preferred Name**: A\n\n\n"
        "## User Memory\n- likes tea\n\n\n"
        "## Entity Memory\n- **igni**: CLI\n\n\n"
        "## Session Context\nRefactoring\n\n\n"
        "## Decision Log\ntwo decisions\n"
    )


async def test_nothing_stored_says_so() -> None:
    result = await _command(
        _Learning({"user_profile": None, "user_memory": Memories(user_id="u1")})
    ).show()

    assert result.content == LearningRecall.EMPTY_MESSAGE


async def test_a_failed_recall_is_an_error_not_an_empty_memory() -> None:
    result = await _command(_Learning(error=RuntimeError("db down"))).show()

    assert result.kind is CommandResultKind.ERROR
    assert "db down" in result.content
    assert result.content != LearningRecall.EMPTY_MESSAGE

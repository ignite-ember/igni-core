"""A session switched to answers its own todo list.

The persisted list was read back only at start-up, so after a switch
``session.todos()`` answered the previous session's list, or none
(campaign R095).
"""

from __future__ import annotations

from types import SimpleNamespace

from igni_core.core.session.core import Session
from igni_core.core.session.event_log import SessionEventLog
from igni_core.core.tools.todo import TodoItemWire, TodoStore


async def _no_events() -> list:
    return []


async def test_the_switch_loads_the_target_session_s_list() -> None:
    rebound: list[str] = []
    stored = {"s2": [TodoItemWire(content="Alpha", status="in_progress")]}
    current = {"id": "s1"}

    async def rebind(session_id: str) -> None:
        rebound.append(session_id)
        current["id"] = session_id

    async def load_todos() -> list[TodoItemWire]:
        return stored.get(current["id"], [])

    session = Session.__new__(Session)
    session.identity = SimpleNamespace(rebind=rebind)  # type: ignore[assignment]
    session.persistence = SimpleNamespace(load_todos=load_todos, load_event_log=_no_events)  # type: ignore[assignment]
    session.todo_store = TodoStore()
    session.event_log_store = SessionEventLog()
    session.todo_store.set([TodoItemWire(content="from s1", status="pending")])

    await session.rebind_identity("s2")
    assert [t.content for t in session.todo_store.items] == ["Alpha"]

    await session.rebind_identity("s3")
    assert session.todo_store.items == []
    assert rebound == ["s2", "s3"]


async def test_the_switch_loads_the_target_session_s_event_log() -> None:
    """R214: the log kept the previous session's views after a switch, so
    the switched-to chat's history showed them and lost its own, and the
    next append saved them into it."""
    from igni_core.core.session.event_log_schema import (
        VisualizationDeltaEntry,
        VisualizationDeltaPayload,
    )

    def card(seq: int, title: str) -> VisualizationDeltaEntry:
        return VisualizationDeltaEntry(
            seq=seq, run_id="r", payload=VisualizationDeltaPayload(spec_id=title, spec_json="{}")
        )

    stored = {"A": [card(4, "Fruit")]}
    current = {"id": "B"}

    async def rebind(session_id: str) -> None:
        current["id"] = session_id

    async def load_todos() -> list[TodoItemWire]:
        return []

    async def load_event_log() -> list:
        return stored.get(current["id"], [])

    session = Session.__new__(Session)
    session.identity = SimpleNamespace(rebind=rebind)  # type: ignore[assignment]
    session.persistence = SimpleNamespace(  # type: ignore[assignment]
        load_todos=load_todos, load_event_log=load_event_log
    )
    session.todo_store = TodoStore()
    session.event_log_store = SessionEventLog()
    session.event_log_store.restore([card(1, "Veg")])

    await session.rebind_identity("A")
    assert [e.payload.spec_id for e in session.event_log] == ["Fruit"]  # type: ignore[union-attr]
    assert session.event_log_store.seq == 4

    await session.rebind_identity("C")
    assert session.event_log == []

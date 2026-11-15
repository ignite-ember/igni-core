"""A session switched to answers its own todo list.

The persisted list was read back only at start-up, so after a switch
``session.todos()`` answered the previous session's list, or none
(campaign R095).
"""

from __future__ import annotations

from types import SimpleNamespace

from igni_core.core.session.core import Session
from igni_core.core.tools.todo import TodoItemWire, TodoStore


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
    session.persistence = SimpleNamespace(load_todos=load_todos)  # type: ignore[assignment]
    session.todo_store = TodoStore()
    session.todo_store.set([TodoItemWire(content="from s1", status="pending")])

    await session.rebind_identity("s2")
    assert [t.content for t in session.todo_store.items] == ["Alpha"]

    await session.rebind_identity("s3")
    assert session.todo_store.items == []
    assert rebound == ["s2", "s3"]

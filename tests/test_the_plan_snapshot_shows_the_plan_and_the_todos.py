"""``PlanSnapshotBuilder`` is what the plan panel and the todos panel read.

Real plan and todo stores behind a session that has nothing else.
"""

from __future__ import annotations

from types import SimpleNamespace

from igni_core.backend.plan_snapshot_builder import PlanSnapshotBuilder
from igni_core.core.tools.plan import PlanStore
from igni_core.core.tools.todo import TodoItemWire, TodoStore


def _builder(plan: PlanStore, todos: TodoStore) -> PlanSnapshotBuilder:
    return PlanSnapshotBuilder(SimpleNamespace(plan_store=plan, todo_store=todos))


def test_the_latest_plan_carries_its_history_its_tasks_and_its_decision():
    plan = PlanStore()
    plan.set_plan("first", run_id="r1")
    plan.set_plan("second", run_id="r2")
    plan.set_decision("r2", "approved")
    todos = TodoStore()
    todos.set([TodoItemWire(content="Run tests", status="pending")])

    result = _builder(plan, todos).latest()

    assert result.latest == "second"
    assert result.history == ["first"]
    assert [t.content for t in result.tasks] == ["Run tests"]
    assert result.state == "approved"


def test_a_session_with_no_plan_has_an_empty_snapshot():
    result = _builder(PlanStore(), TodoStore()).latest()

    assert (result.latest, result.history, result.tasks, result.state) == ("", [], [], "")


def test_the_history_handed_out_is_a_copy():
    plan = PlanStore()
    plan.set_plan("first")
    plan.set_plan("second")

    _builder(plan, TodoStore()).latest().history.append("tampered")

    assert plan.history == ["first"]


def test_the_todos_are_whatever_todo_write_last_published():
    todos = TodoStore()
    builder = _builder(PlanStore(), todos)
    assert builder.todos() == []

    todos.set(
        [
            TodoItemWire(content="a", status="in_progress"),
            TodoItemWire(content="b", status="pending"),
        ]
    )

    assert [(t.content, t.status) for t in builder.todos()] == [
        ("a", "in_progress"),
        ("b", "pending"),
    ]

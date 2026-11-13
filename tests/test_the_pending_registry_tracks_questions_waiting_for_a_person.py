"""The registry of sub-agent questions: what is shown, once, and what is ready to continue."""

from __future__ import annotations

from types import SimpleNamespace

from agno.models.response import ToolExecution
from agno.run.requirement import RunRequirement

from igni_core.core.hitl.models import DetailUpdate, PendingEntry
from igni_core.core.hitl.registry import PendingRegistry


def _entry(run_id: str = "r1", *, call: str = "c", answered: bool = False) -> PendingEntry:
    entry = PendingEntry(
        requirement=RunRequirement(
            tool_execution=ToolExecution(
                tool_call_id=call, tool_name="run_shell_command", tool_args={"command": call}
            )
        ),
        run_id=run_id,
    )
    if answered:
        entry.event.set()
    return entry


def test_questions_are_found_listed_in_arrival_order_and_removed_once():
    registry = PendingRegistry()
    first, second = _entry(call="a"), _entry(call="b")
    registry.add("q1", first)
    registry.add("q2", second)

    assert registry.ids() == ["q1", "q2"]
    assert registry.entry("q1") is first
    assert registry.entry("nope") is None
    assert registry.remove("q1") is first
    assert registry.remove("q1") is None
    assert registry.ids() == ["q2"]


def test_adding_an_id_again_replaces_the_question():
    registry = PendingRegistry()
    registry.add("q1", _entry(call="a"))
    newer = _entry(call="b")

    registry.add("q1", newer)

    assert registry.ids() == ["q1"]
    assert registry.entry("q1") is newer


def test_waiting_count_counts_only_awaited_open_questions_that_still_exist():
    registry = PendingRegistry()
    registry.add("open", _entry(call="a"))
    registry.add("answered", _entry(call="b", answered=True))
    registry.add("unawaited", _entry(call="c"))
    registry.add("removed", _entry(call="d"))
    for requirement_id in ("open", "answered", "removed"):
        registry.mark_awaited(requirement_id, True)
    registry.mark_awaited("never-added", True)
    registry.remove("removed")

    assert registry.waiting_count == 1

    registry.mark_awaited("open", False)
    registry.mark_awaited("open", False)
    assert registry.waiting_count == 0


def test_has_unresolved_is_true_until_every_question_is_answered():
    registry = PendingRegistry()
    assert registry.has_unresolved is False

    done = _entry(call="a", answered=True)
    waiting = _entry(call="b")
    registry.add("q1", done)
    assert registry.has_unresolved is False
    registry.add("q2", waiting)
    assert registry.has_unresolved is True

    waiting.event.set()
    assert registry.has_unresolved is False


def test_unsurfaced_questions_are_taken_once_and_the_arrival_signal_is_cleared():
    registry = PendingRegistry()
    registry.add("q1", _entry(call="a"))
    registry.add("q2", _entry(call="b", answered=True))
    registry.new_arrival.set()

    taken = registry.take_unsurfaced()

    assert [h.requirement_id for h in taken] == ["q1"]
    assert taken[0].entry is registry.entry("q1")
    assert registry.entry("q1").surfaced is True
    assert registry.new_arrival.is_set() is False
    assert registry.take_unsurfaced() == []


def test_handing_back_makes_the_questions_show_again_and_wakes_the_relay():
    registry = PendingRegistry()
    registry.add("q1", _entry(call="a"))
    taken = registry.take_unsurfaced()
    assert registry.new_arrival.is_set() is False

    registry.hand_back(taken)

    assert registry.new_arrival.is_set() is True
    assert [h.requirement_id for h in registry.take_unsurfaced()] == ["q1"]


def test_handing_back_nothing_still_wakes_the_relay():
    registry = PendingRegistry()

    registry.hand_back([])

    assert registry.new_arrival.is_set() is True


def test_updates_are_returned_once_and_only_for_questions_still_open():
    registry = PendingRegistry()
    registry.add("open", _entry(call="a"))
    registry.add("answered", _entry(call="b", answered=True))
    for requirement_id in ("open", "answered", "gone"):
        registry.queue_update(DetailUpdate(requirement_id=requirement_id, details=requirement_id))

    updates = registry.take_updates()

    assert [(u.requirement_id, u.details) for u in updates] == [("open", "open")]
    assert registry.take_updates() == []


def test_the_request_for_an_id_is_what_its_entry_shows_or_none():
    registry = PendingRegistry()
    registry.add("q1", _entry(call="a"))

    request = registry.request_for("q1")

    assert request is not None
    assert (request.requirement_id, request.tool_name) == ("q1", "run_shell_command")
    assert registry.request_for("nope") is None


def test_waiting_requests_group_the_open_questions_by_run_and_skip_the_answered():
    registry = PendingRegistry()
    registry.add("q1", _entry("r1", call="a"))
    registry.add("q2", _entry("r2", call="b"))
    registry.add("q3", _entry("r1", call="c"))
    registry.add("q4", _entry("r2", call="d", answered=True))
    registry.add("q5", _entry("r3", call="e", answered=True))

    paused = registry.waiting_requests()

    assert [(p.run_id, [r.requirement_id for r in p.requirements]) for p in paused] == [
        ("r1", ["q1", "q3"]),
        ("r2", ["q2"]),
    ]
    assert PendingRegistry().waiting_requests() == []


def _restore(registry: PendingRegistry, requirement_id: str, entry: PendingEntry) -> None:
    registry.add(requirement_id, entry)
    registry.mark_restored(requirement_id)


def test_a_restored_run_is_ready_only_when_all_its_approvals_are_answered():
    registry = PendingRegistry()
    answered = _entry("r1", call="a", answered=True)
    waiting = _entry("r1", call="b")
    _restore(registry, "q1", answered)
    _restore(registry, "q2", waiting)

    assert registry.take_ready() == []
    assert registry.ids() == ["q1", "q2"]

    waiting.event.set()
    ready = registry.take_ready()

    assert [r.run_id for r in ready] == ["r1"]
    assert {r.tool_execution.tool_call_id for r in ready[0].requirements} == {"a", "b"}
    assert len(ready[0].requirements) == 2
    assert ready[0].link is None
    assert registry.ids() == []
    assert registry.take_ready() == []


def test_a_question_that_was_not_restored_is_never_handed_back_as_a_run():
    registry = PendingRegistry()
    registry.add("q1", _entry("r1", answered=True))

    assert registry.take_ready() == []
    assert registry.ids() == ["q1"]


def test_ready_runs_are_independent_and_leave_the_unready_ones_in_place():
    registry = PendingRegistry()
    _restore(registry, "q1", _entry("r1", call="a", answered=True))
    _restore(registry, "q2", _entry("r2", call="b"))
    _restore(registry, "q3", _entry("r3", call="c", answered=True))
    _restore(registry, "gone", _entry("r4", call="d", answered=True))
    registry.remove("gone")

    ready = registry.take_ready()

    assert sorted(r.run_id for r in ready) == ["r1", "r3"]
    assert registry.ids() == ["q2"]


def test_a_safety_question_is_continued_with_the_run_but_is_not_one_of_its_requirements():
    registry = PendingRegistry()
    agno = _entry("r1", call="a", answered=True)
    safety = PendingEntry(requirement=SimpleNamespace(tool_execution=None), run_id="r1")
    safety.event.set()
    _restore(registry, "q1", agno)
    _restore(registry, "q2", safety)

    ready = registry.take_ready()

    assert len(ready) == 1
    assert ready[0].requirements == [agno.requirement]
    assert registry.ids() == []

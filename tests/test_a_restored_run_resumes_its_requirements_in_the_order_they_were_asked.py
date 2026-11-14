"""A run read back after a restart is continued with its requirements, and
its runs, in the order they were asked — not in a set's order, which is
different in every process."""

from __future__ import annotations

from agno.models.response import ToolExecution
from agno.run.requirement import RunRequirement

from igni_core.core.hitl.models import PendingEntry
from igni_core.core.hitl.registry import PendingRegistry


def _entry(call_id: str, run_id: str) -> PendingEntry:
    requirement = RunRequirement(
        tool_execution=ToolExecution(
            tool_call_id=call_id, tool_name="run_shell_command", requires_confirmation=True
        )
    )
    entry = PendingEntry(requirement=requirement, run_id=run_id)
    entry.event.set()
    return entry


def test_the_requirements_come_back_in_arrival_order():
    registry = PendingRegistry()
    calls = [f"call-{n}" for n in range(12)]
    for n, call in enumerate(calls):
        registry.add(f"id-{n:02d}-{call}", _entry(call, "run-b" if n % 2 else "run-a"))
        registry.mark_restored(f"id-{n:02d}-{call}")

    ready = registry.take_ready()

    assert [run.run_id for run in ready] == ["run-a", "run-b"]
    by_run = {
        run.run_id: [r.tool_execution.tool_call_id for r in run.requirements] for run in ready
    }
    assert by_run["run-a"] == [c for n, c in enumerate(calls) if n % 2 == 0]
    assert by_run["run-b"] == [c for n, c in enumerate(calls) if n % 2 == 1]

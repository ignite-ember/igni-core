"""``SessionActivity.of`` and ``Activity.of``: running, waiting or idle.

What decides whether a VM may sleep, so the boundary between a run that
works and a run that only waits on a person is pinned cell by cell.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from igni_core.backend.activity import Activity, ActivityState, SessionActivity
from igni_core.core.activity.registry import ActivityRegistry, BusyKind

RUNNING, WAITING, IDLE = ActivityState.RUNNING, ActivityState.WAITING, ActivityState.IDLE


@pytest.mark.parametrize(
    ("blocked", "active", "state"),
    [
        (0, 0, RUNNING),  # a run with no sub-agents
        (0, 2, RUNNING),  # sub-agents, none blocked
        (1, 2, RUNNING),  # one blocked, one still working
        (2, 2, WAITING),  # every sub-agent run is blocked on a person
        (3, 2, WAITING),  # more blocked than counted active
        (1, 0, WAITING),  # blocked with no active count
    ],
)
def test_a_run_in_progress_only_waits_when_every_subagent_run_is_blocked(blocked, active, state):
    session = SessionActivity.of(
        "s",
        run_in_progress=True,
        waiting_approvals=0,
        blocked_spawns=blocked,
        active_subagent_runs=active,
    )

    assert session.state is state


def _session(*, run: bool, approvals: int) -> SessionActivity:
    return SessionActivity.of(
        "s",
        run_in_progress=run,
        waiting_approvals=approvals,
        blocked_spawns=0,
        active_subagent_runs=0,
    )


def test_no_run_but_approvals_waiting_is_waiting_and_counts_them():
    session = _session(run=False, approvals=2)

    assert session.state is WAITING
    assert session.waiting_approvals == 2


def test_no_run_and_nothing_waiting_is_idle():
    assert _session(run=False, approvals=0).state is IDLE


def test_a_run_in_progress_is_running_even_with_approvals_waiting():
    session = _session(run=True, approvals=1)

    assert session.state is RUNNING
    assert session.waiting_approvals == 1


def test_no_sessions_and_no_leases_is_idle_and_suspendable():
    activity = Activity.of([])

    assert activity.state is IDLE
    assert activity.suspendable
    assert activity.busy == []
    assert activity.wake_at is None


def test_any_running_session_makes_the_backend_running_and_not_suspendable():
    activity = Activity.of([_session(run=False, approvals=1), _session(run=True, approvals=0)])

    assert activity.state is RUNNING
    assert not activity.suspendable


def test_waiting_sessions_sum_their_approvals_and_leave_the_backend_suspendable():
    activity = Activity.of([_session(run=False, approvals=1), _session(run=False, approvals=2)])

    assert activity.state is WAITING
    assert activity.suspendable
    assert activity.waiting_approvals == 3


def test_a_busy_lease_and_a_wake_come_from_the_registry():
    registry = ActivityRegistry()
    wake = datetime(2030, 1, 1, tzinfo=UTC)
    registry.wake("scheduler", wake, "a task is due")
    idle = [_session(run=False, approvals=0)]

    assert Activity.now(idle, registry).state is IDLE
    assert Activity.now(idle, registry).wake_at == wake
    with registry.hold(BusyKind.MODEL_CALL, "model", "a call out"):
        activity = Activity.now(idle, registry)

    assert activity.state is RUNNING
    assert not activity.suspendable
    assert [lease.reason for lease in activity.busy] == ["a call out"]

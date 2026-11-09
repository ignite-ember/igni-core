"""A headless plan-mode run prints the plan instead of swallowing it.

``--read-only`` selects ``PermissionMode.PLAN``. A denied edit tells the model
to "call ``exit_plan_mode(plan)`` when ready to execute", which writes the plan
to :class:`PlanStore`. The only consumer of that store is the frontend's plan
card: ``approve_plan`` is an FE operation and ``/plan`` is an interactive slash
command.

So a headless ``--read-only`` run produced a plan that nothing could read, and
then ended. From outside it looked like the agent had declined to do the work.
Worse, the advice in the denial message was unfollowable — there was no second
turn for an approval to unblock, because a single-message run is one-shot.

Printing is the right resolution rather than auto-approving. The user asked for
a read-only session; executing the plan because nobody was there to say no
would be the opposite of the flag.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import patch

from igni_core.core.config.settings import Settings
from igni_core.core.session.single_message_run import SingleMessageRun
from igni_core.core.tools.plan.store import PlanStore


@dataclass
class _Display:
    """The prints of one run, in order."""

    printed: list[str] = field(default_factory=list)

    def print_info(self, text: str) -> None:
        self.printed.append(text)


@dataclass
class _Session:
    """The parts of a session the plan reporting reads.

    A real :class:`~igni_core.core.session.core.Session` always carries a
    :class:`PlanStore` — the store is what ``exit_plan_mode`` writes to — so
    the double carries one too.
    """

    plan_store: PlanStore
    display: _Display


def _run_with(plan: str) -> list[str]:
    """Drive ``_emit_pending_plan`` against a session holding ``plan``."""
    store = PlanStore()
    if plan:
        store.set_plan(plan)
    session = _Session(plan_store=store, display=_Display())

    with patch("igni_core.core.session.session_run.Session", return_value=session):
        run = SingleMessageRun(Settings())

    run._emit_pending_plan()
    return session.display.printed


class TestThePlanReachesTheUser:
    def test_a_submitted_plan_is_printed(self):
        out = _run_with("1. Rename the thing\n2. Update callers")

        assert out, "the plan was produced and never shown"
        assert "Rename the thing" in out[0]
        assert "Update callers" in out[0]

    def test_the_output_says_nothing_ran(self):
        """The plan alone is ambiguous — it reads like a summary of work done."""
        out = _run_with("1. Rename the thing")

        assert "nothing was executed" in out[0]

    def test_it_says_how_to_carry_the_plan_out(self):
        out = _run_with("1. Rename the thing")

        assert "--read-only" in out[0]


class TestItStaysQuietOtherwise:
    def test_no_plan_prints_nothing(self):
        assert _run_with("") == []

    def test_a_blank_plan_prints_nothing(self):
        """Whitespace is not a plan: printing it would report an outcome
        nobody produced."""
        assert _run_with("   \n  ") == []

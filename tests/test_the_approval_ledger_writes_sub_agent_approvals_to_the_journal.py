"""The sub-agent approvals' durable side, driven against a real journal on SQLite."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from agno.models.response import ToolExecution
from agno.run.requirement import RunRequirement

from igni_core.core.approvals.journal import ApprovalJournal
from igni_core.core.approvals.records import ApprovalOrigin
from igni_core.core.hitl.ledger import ApprovalLedger
from igni_core.core.hitl.models import PendingEntry
from igni_core.core.hitl.turns import TurnLedger
from igni_core.core.tools.spawn_context import ParentCall, SpawnCalls
from igni_core.protocol import messages as msg


def _entry(run_id: str = "sub-run", call: str = "c1") -> PendingEntry:
    return PendingEntry(
        requirement=RunRequirement(
            tool_execution=ToolExecution(
                tool_call_id=call,
                tool_name="run_shell_command",
                tool_args={"command": call},
                requires_confirmation=True,
            )
        ),
        run_id=run_id,
        agent_path=["architect"],
    )


def _parent(run_id: str) -> ParentCall:
    return ParentCall(
        agent_name="main",
        run_id=run_id,
        session_id="s1",
        tool_call_id=f"call-{run_id}",
        tool_name="spawn_agent",
    )


@pytest.fixture
def journal(tmp_path: Path) -> ApprovalJournal:
    return ApprovalJournal(tmp_path / "state.db")


@pytest.fixture
def attached(journal: ApprovalJournal) -> tuple[ApprovalLedger, TurnLedger]:
    turns = TurnLedger()
    ledger = ApprovalLedger(turns)
    ledger.attach(journal, lambda: "s1")
    return ledger, turns


def test_without_a_journal_every_call_is_a_no_op_and_reads_empty():
    ledger = ApprovalLedger(TurnLedger())

    ledger.record("q1", _entry())
    ledger.update_request("q1", msg.HITLRequest(requirement_id="q1"))
    ledger.forget("q1")
    ledger.forget_spawn("sub-run")
    with SpawnCalls.entered(_parent("main-run")):
        ledger.record_spawn("sub-run", "agent-session", ["architect"])

    assert ledger.restore("s1") == []
    assert ledger.spawn_link("sub-run") is None
    assert ledger.take_interrupted() == []


def test_a_recorded_approval_is_read_back_under_the_live_session_id_with_its_request(
    journal: ApprovalJournal,
):
    current = ["s1"]
    ledger = ApprovalLedger(TurnLedger())
    ledger.attach(journal, lambda: current[0])

    ledger.record("q1", _entry())
    current[0] = "s2"
    ledger.record("q2", _entry(call="c2"))

    [stored] = ledger.restore("s1")
    assert (stored.requirement_id, stored.run_id, stored.origin) == (
        "q1",
        "sub-run",
        ApprovalOrigin.SUB_AGENT,
    )
    assert stored.requirement.tool_execution.tool_args == {"command": "c1"}
    assert stored.request is not None
    assert stored.request.agent_path == ["architect"]
    assert [s.requirement_id for s in ledger.restore("s2")] == ["q2"]
    assert ledger.restore("other") == []


def test_a_safety_question_is_never_journalled(attached, journal: ApprovalJournal):
    ledger, _ = attached
    safety = PendingEntry(
        requirement=SimpleNamespace(tool_execution=None, details="", details_pending=False),
        run_id="sub-run",
    )

    ledger.record("q1", safety)

    assert journal.waiting("s1") == []


def test_forget_and_update_request_reach_the_journal(attached):
    ledger, _ = attached
    ledger.record("q1", _entry())
    ledger.record("q2", _entry(call="c2"))

    ledger.update_request("q1", msg.HITLRequest(requirement_id="q1", details="better"))
    ledger.forget("q2")

    [stored] = ledger.restore("s1")
    assert stored.requirement_id == "q1"
    assert stored.request is not None and stored.request.details == "better"


def test_a_spawn_is_stored_named_by_the_leaf_agent_and_links_every_parent_run(
    attached, journal: ApprovalJournal
):
    ledger, turns = attached
    turns.tool_finished("main-run", "done", "output")

    with SpawnCalls.entered(_parent("main-run")), SpawnCalls.entered(_parent("mid-run")):
        ledger.record_spawn("sub-run", "agent-session", ["architect", "reviewer"])

    link = ledger.spawn_link("sub-run")
    assert link is not None
    assert (link.session_id, link.agent_session_id, link.agent_name) == (
        "s1",
        "agent-session",
        "reviewer",
    )
    assert [p.run_id for p in link.parents] == ["mid-run", "main-run"]
    assert journal.turns.of_run("main-run") == {"done": "output"}
    turns.tool_finished("mid-run", "later", "x")
    assert journal.turns.of_run("mid-run") == {"later": "x"}


@pytest.mark.parametrize(
    ("parents", "agent_session_id", "agent_path"),
    [
        (False, "agent-session", ["architect"]),
        (True, "", ["architect"]),
        (True, "agent-session", []),
    ],
    ids=["no parent call", "no agent session", "no agent path"],
)
def test_a_spawn_without_what_continuing_it_needs_is_not_stored(
    attached, parents: bool, agent_session_id: str, agent_path: list[str]
):
    ledger, turns = attached
    turns.tool_finished("main-run", "done", "output")

    if parents:
        with SpawnCalls.entered(_parent("main-run")):
            ledger.record_spawn("sub-run", agent_session_id, agent_path)
    else:
        ledger.record_spawn("sub-run", agent_session_id, agent_path)

    assert ledger.spawn_link("sub-run") is None
    assert turns.results("main-run") == {}


def test_forget_spawn_drops_the_link(attached):
    ledger, _ = attached
    with SpawnCalls.entered(_parent("main-run")):
        ledger.record_spawn("sub-run", "agent-session", ["architect"])

    ledger.forget_spawn("sub-run")

    assert ledger.spawn_link("sub-run") is None


def test_restore_returns_only_sub_agent_approvals_and_keeps_aside_links_with_none_waiting(
    attached, journal: ApprovalJournal
):
    ledger, _ = attached
    ledger.record("q1", _entry("waiting-run"))
    journal.record(
        requirement_id="main-q",
        session_id="s1",
        run_id="main-run",
        origin=ApprovalOrigin.MAIN,
        requirement=_entry().requirement,
    )
    with SpawnCalls.entered(_parent("main-run")):
        ledger.record_spawn("waiting-run", "agent-a", ["architect"])
        ledger.record_spawn("cut-run", "agent-b", ["reviewer"])
        # A main-agent approval of the same run does not count as asked by the sub-agent.
        ledger.record_spawn("main-run", "agent-c", ["tester"])

    restored = ledger.restore("s1")

    assert [s.requirement_id for s in restored] == ["q1"]
    interrupted = ledger.take_interrupted()
    assert sorted(link.run_id for link in interrupted) == ["cut-run", "main-run"]
    assert ledger.take_interrupted() == []


def test_a_restore_for_a_session_with_nothing_left_has_nothing_interrupted(attached):
    ledger, _ = attached
    with SpawnCalls.entered(_parent("main-run")):
        ledger.record_spawn("cut-run", "agent-b", ["reviewer"])
    ledger.restore("s1")

    assert ledger.restore("elsewhere") == []

    assert ledger.take_interrupted() == []

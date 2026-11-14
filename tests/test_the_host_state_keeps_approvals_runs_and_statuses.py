"""The host's mirrors: approvals waiting, each session's runs and edits, the last status.

Each is driven alone, as the broadcasts and the members drive it.
"""

from __future__ import annotations

import pytest

from igni_core.extensions.host.state.approvals import Approval, ApprovalMirror
from igni_core.extensions.host.state.runs import RunLedger
from igni_core.extensions.host.state.status import StatusMemory
from igni_core.protocol import messages as msg


def request(requirement_id: str, **kw: object) -> msg.HITLRequest:
    fields: dict[str, object] = {
        "requirement_id": requirement_id,
        "tool_name": "Bash",
        "friendly_name": "Run a command",
        "tool_args": {"command": "ls"},
        "details": "",
        "agent_path": [],
    }
    fields.update(kw)
    return msg.HITLRequest(**fields)  # type: ignore[arg-type]


def paused(session_id: str, *ids: str) -> msg.RunPaused:
    return msg.RunPaused(session_id=session_id, requirements=[request(i) for i in ids])


class Backend:
    def __init__(self, answer: list[msg.Message] | None = None) -> None:
        self.answer = answer or []
        self.loads = 0

    def load(self) -> list[msg.Message]:
        self.loads += 1
        return self.answer


class TestApproval:
    def test_it_is_built_from_the_request_and_session(self) -> None:
        a = Approval.requested(
            "s1",
            request("r1", tool_name="Edit", friendly_name="Edit", details="d", agent_path=["a"]),
        )
        assert (a.requirement_id, a.session_id, a.tool_name, a.friendly_name, a.details) == (
            "r1",
            "s1",
            "Edit",
            "Edit",
            "d",
        )
        assert a.agent_path == ["a"]
        assert Approval.WIRE == "chat.approvals.requested"

    def test_later_changes_to_the_request_do_not_alter_it(self) -> None:
        r = request("r1", agent_path=["a"], tool_args={"k": "é"})
        a = Approval.requested("s1", r)
        r.tool_args["k"] = "changed"
        r.agent_path.append("b")
        assert a.tool_args == {"k": "é"}
        assert a.agent_path == ["a"]


class TestApprovalMirror:
    def test_a_session_never_heard_of_has_nothing_waiting(self) -> None:
        m = ApprovalMirror()
        assert (m.waiting("x"), m.has("x", "r"), m.knows("x")) == ([], False, False)
        m.resolve("x", "r")
        m.set_details("x", "r", "d")
        m.clear("x")
        assert not m.knows("x")

    def test_approvals_wait_in_arrival_order_and_a_repeat_replaces(self) -> None:
        m = ApprovalMirror()
        m.record("s", request("b"))
        m.record("s", request("a"))
        m.record("s", request("b", details="again"))
        assert [(a.requirement_id, a.details) for a in m.waiting("s")] == [
            ("b", "again"),
            ("a", ""),
        ]
        assert m.has("s", "a") and not m.has("s", "zzz")
        assert m.knows("s")

    def test_resolving_removes_one_and_leaves_the_session_known(self) -> None:
        m = ApprovalMirror()
        m.record("s", request("a"))
        m.record("s", request("b"))
        m.resolve("s", "a")
        m.resolve("s", "nope")
        assert [a.requirement_id for a in m.waiting("s")] == ["b"]
        m.resolve("s", "b")
        assert m.waiting("s") == [] and m.knows("s")

    def test_details_change_only_for_a_waiting_approval(self) -> None:
        m = ApprovalMirror()
        m.record("s", request("a"))
        m.set_details("s", "a", "new")
        m.set_details("s", "missing", "x")
        assert [(a.requirement_id, a.details) for a in m.waiting("s")] == [("a", "new")]

    def test_restore_takes_paused_requirements_and_ignores_other_messages(self) -> None:
        m = ApprovalMirror()
        m.restore("s", [paused("s", "a", "b"), msg.StatusUpdate(session_id="s")])
        assert [a.requirement_id for a in m.waiting("s")] == ["a", "b"]

    def test_restore_keeps_what_is_already_known(self) -> None:
        m = ApprovalMirror()
        m.record("s", request("a"))
        m.set_details("s", "a", "kept")
        m.restore("s", [paused("s", "a", "b")])
        assert [(a.requirement_id, a.details) for a in m.waiting("s")] == [("a", "kept"), ("b", "")]

    def test_restoring_nothing_still_makes_the_session_known(self) -> None:
        m = ApprovalMirror()
        m.restore("s", [])
        assert m.knows("s") and m.waiting("s") == []
        m.restore("t", [msg.RunPaused(session_id="t")])
        assert m.knows("t") and m.waiting("t") == []

    def test_ensure_reads_the_backend_once_and_returns_itself(self) -> None:
        m = ApprovalMirror()
        backend = Backend([paused("s", "a")])
        assert m.ensure("s", backend.load) is m
        assert m.ensure("s", backend.load) is m
        assert backend.loads == 1
        assert [a.requirement_id for a in m.waiting("s")] == ["a"]

    def test_ensure_does_not_ask_for_a_session_that_was_recorded(self) -> None:
        m = ApprovalMirror()
        m.record("s", request("a"))
        backend = Backend()
        m.ensure("s", backend.load)
        assert backend.loads == 0

    def test_clear_forgets_the_session_so_the_backend_is_asked_again(self) -> None:
        m = ApprovalMirror()
        backend = Backend([paused("s", "a")])
        m.ensure("s", backend.load)
        m.clear("s")
        assert not m.knows("s") and m.waiting("s") == []
        m.ensure("s", backend.load)
        assert backend.loads == 2

    def test_a_failing_load_leaves_the_session_unknown(self) -> None:
        m = ApprovalMirror()

        def boom() -> list[msg.Message]:
            raise RuntimeError("db down")

        with pytest.raises(RuntimeError):
            m.ensure("s", boom)
        assert not m.knows("s")

    def test_sessions_are_kept_apart(self) -> None:
        m = ApprovalMirror()
        m.record("s1", request("a"))
        assert m.waiting("s2") == [] and not m.has("s2", "a")


class TestRunLedger:
    def started(self, ledger: RunLedger, session: str, run: str, parent: str = "") -> None:
        ledger.started(msg.RunStarted(session_id=session, run_id=run, parent_run_id=parent))

    def test_a_session_with_no_run_has_none_and_no_files(self) -> None:
        ledger = RunLedger()
        assert ledger.last_run_of("s") == ""
        assert ledger.finished("s") == frozenset()
        ledger.dropped("s")

    def test_a_top_level_run_is_the_last_and_empties_the_files(self) -> None:
        ledger = RunLedger()
        self.started(ledger, "s", "r1")
        ledger.edited("s", "a.py")
        self.started(ledger, "s", "r2")
        assert ledger.last_run_of("s") == "r2"
        assert ledger.finished("s") == frozenset()

    def test_a_sub_agent_run_changes_nothing(self) -> None:
        ledger = RunLedger()
        self.started(ledger, "s", "r1")
        ledger.edited("s", "a.py")
        self.started(ledger, "s", "child", parent="r1")
        assert ledger.last_run_of("s") == "r1"
        assert ledger.finished("s") == frozenset({"a.py"})

    def test_a_file_edited_often_counts_once_and_is_given_once(self) -> None:
        ledger = RunLedger()
        self.started(ledger, "s", "r1")
        for name in ("a.py", "a.py", "b/é.py"):
            ledger.edited("s", name)
        assert ledger.finished("s") == frozenset({"a.py", "b/é.py"})
        assert ledger.finished("s") == frozenset()

    def test_an_edit_before_any_run_is_kept(self) -> None:
        ledger = RunLedger()
        ledger.edited("s", "a.py")
        assert ledger.finished("s") == frozenset({"a.py"})

    def test_dropping_forgets_the_files_but_not_the_last_run(self) -> None:
        ledger = RunLedger()
        self.started(ledger, "s", "r1")
        ledger.edited("s", "a.py")
        ledger.dropped("s")
        assert ledger.finished("s") == frozenset()
        assert ledger.last_run_of("s") == "r1"

    def test_sessions_are_kept_apart(self) -> None:
        ledger = RunLedger()
        self.started(ledger, "s1", "r1")
        self.started(ledger, "s2", "r2")
        ledger.edited("s1", "a.py")
        assert ledger.last_run_of("s2") == "r2"
        assert ledger.finished("s2") == frozenset()
        assert ledger.finished("s1") == frozenset({"a.py"})


class TestStatusMemory:
    def test_the_first_status_of_a_session_replaces_nothing(self) -> None:
        assert StatusMemory().remember(msg.StatusUpdate(session_id="s")) is None

    def test_a_later_status_gives_back_the_one_it_replaced(self) -> None:
        memory = StatusMemory()
        first = msg.StatusUpdate(session_id="s", input_tokens=1)
        second = msg.StatusUpdate(session_id="s", input_tokens=2)
        memory.remember(first)
        assert memory.remember(second) == first
        assert memory.remember(second) == second

    def test_sessions_do_not_replace_each_other(self) -> None:
        memory = StatusMemory()
        memory.remember(msg.StatusUpdate(session_id="a", input_tokens=1))
        assert memory.remember(msg.StatusUpdate(session_id="b")) is None
        assert memory.remember(msg.StatusUpdate(session_id="")) is None

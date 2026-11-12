"""``PendingRequirementsStore`` holds the approvals waiting on a person and the
ones the policy already decided, and writes both through to the journal.

Real store; the restart tests use a real ``ApprovalJournal`` on SQLite and a
second store over the same file, which is what a restart is.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agno.models.response import ToolExecution
from agno.run.requirement import RunRequirement

from igni_core.backend.pending_requirements_store import PendingRequirementsStore
from igni_core.backend.schemas_pause import PendingRequirement
from igni_core.core.approvals.journal import ApprovalJournal
from igni_core.protocol import messages as msg

SESSION = "s1"


def _requirement(call_id: str) -> RunRequirement:
    return RunRequirement(
        tool_execution=ToolExecution(
            tool_call_id=call_id,
            tool_name="run_shell_command",
            tool_args={"command": call_id},
            requires_confirmation=True,
        )
    )


def _entry(call_id: str, run_id: str | None = "run-1", *, asked: bool = True) -> PendingRequirement:
    request = (
        msg.HITLRequest(
            requirement_id=call_id, tool_name="run_shell_command", friendly_name="Shell"
        )
        if asked
        else None
    )
    return PendingRequirement(req=_requirement(call_id), run_id=run_id, request=request)


@pytest.fixture
def journal(tmp_path: Path) -> ApprovalJournal:
    return ApprovalJournal(tmp_path / "approvals.db")


def _durable(journal: ApprovalJournal) -> PendingRequirementsStore:
    return PendingRequirementsStore(journal, session_id=lambda: SESSION)


def test_a_registered_requirement_is_popped_once_and_unknown_ids_give_none():
    store = PendingRequirementsStore()
    entry = _entry("a")
    store.register("a", entry)

    assert store.pending_ids() == ["a"]
    assert store.pop("a") is entry
    assert store.pop("a") is None
    assert store.pop("never") is None
    assert store.pending_ids() == []


def test_the_pending_ids_are_a_snapshot():
    store = PendingRequirementsStore()
    store.register("a", _entry("a"))

    store.pending_ids().append("b")

    assert store.pending_ids() == ["a"]


def test_auto_resolved_requirements_are_drained_once_per_run():
    store = PendingRequirementsStore()
    first, second = _requirement("1"), _requirement("2")
    store.stash_auto_resolved("run-1", [first])
    store.stash_auto_resolved("run-1", [second])

    assert store.auto_resolved_for("run-1") == [first, second]
    assert store.drain_auto_resolved("run-1") == [first, second]
    assert store.drain_auto_resolved("run-1") == []


def test_draining_a_run_that_is_none_or_unknown_gives_an_empty_list():
    store = PendingRequirementsStore()
    store.stash_auto_resolved("run-1", [_requirement("1")])

    assert store.drain_auto_resolved(None) == []
    assert store.drain_auto_resolved("other") == []
    assert len(store.auto_resolved_for("run-1")) == 1


def test_stashing_nothing_does_not_create_a_bucket():
    store = PendingRequirementsStore()
    store.stash_auto_resolved("run-1", [])

    assert store.auto_resolved_for("run-1") == []


def test_the_auto_resolved_list_handed_out_is_a_copy():
    store = PendingRequirementsStore()
    store.stash_auto_resolved("run-1", [_requirement("1")])

    store.auto_resolved_for("run-1").clear()

    assert len(store.auto_resolved_for("run-1")) == 1


def test_a_finished_run_drops_its_pending_and_auto_resolved_entries_and_counts_them():
    store = PendingRequirementsStore()
    store.register("a", _entry("a", "run-1"))
    store.register("b", _entry("b", "run-1"))
    store.register("c", _entry("c", "run-2"))
    store.stash_auto_resolved("run-1", [_requirement("auto")])

    assert store.sweep_run("run-1") == 2

    assert store.pending_ids() == ["c"]
    assert store.auto_resolved_for("run-1") == []


def test_sweeping_an_unknown_run_drops_nothing():
    store = PendingRequirementsStore()
    store.register("a", _entry("a", "run-1"))

    assert store.sweep_run("other") == 0
    assert store.pending_ids() == ["a"]


def test_waiting_messages_show_one_paused_message_per_run():
    store = PendingRequirementsStore()
    store.register("a", _entry("a", "run-1"))
    store.register("b", _entry("b", "run-2"))
    store.register("c", _entry("c", "run-1"))

    shown = {m.run_id: [r.requirement_id for r in m.requirements] for m in store.waiting_messages()}

    assert shown == {"run-1": ["a", "c"], "run-2": ["b"]}


def test_an_entry_that_was_never_shown_is_not_shown_again_and_a_missing_run_groups_under_empty():
    store = PendingRequirementsStore()
    store.register("quiet", _entry("quiet", asked=False))
    store.register("loose", _entry("loose", run_id=None))

    (only,) = store.waiting_messages()
    assert only.run_id == ""
    assert [r.requirement_id for r in only.requirements] == ["loose"]


def test_without_a_journal_there_is_nothing_to_restore_or_continue():
    store = PendingRequirementsStore()
    store.register("a", _entry("a"))

    assert store.restore(SESSION) == 0
    store.begin_continuing("run-1")
    store.end_continuing("run-1")
    assert store.take_interrupted_runs() == []


def test_a_waiting_approval_is_restored_after_a_restart_and_gone_once_answered(journal):
    before = _durable(journal)
    before.register("a", _entry("a", "run-1"))
    before.register("b", _entry("b", "run-1"))
    before.pop("b")

    after = _durable(journal)
    assert after.restore(SESSION) == 1

    assert after.pending_ids() == ["a"]
    (shown,) = after.waiting_messages()
    assert shown.run_id == "run-1"
    assert shown.requirements[0].requirement_id == "a"
    restored = after.pop("a")
    assert restored is not None and restored.run_id == "run-1"


def test_a_requirement_that_is_not_a_run_requirement_is_kept_in_memory_only(journal):
    before = _durable(journal)
    before.register("a", PendingRequirement(req=object(), run_id="run-1"))

    assert before.pending_ids() == ["a"]
    assert _durable(journal).restore(SESSION) == 0


def test_a_restored_run_with_no_id_comes_back_without_one(journal):
    _durable(journal).register("a", _entry("a", run_id=None))

    after = _durable(journal)
    after.restore(SESSION)

    entry = after.pop("a")
    assert entry is not None and entry.run_id is None


def test_restoring_only_reads_the_asked_session(journal):
    _durable(journal).register("a", _entry("a"))

    assert PendingRequirementsStore(journal).restore("another") == 0


def test_auto_resolved_requirements_survive_a_restart_and_leave_with_their_drain(journal):
    before = _durable(journal)
    before.stash_auto_resolved("run-1", [_requirement("auto")])

    after = _durable(journal)
    assert after.restore(SESSION) == 0
    assert len(after.auto_resolved_for("run-1")) == 1

    assert len(after.drain_auto_resolved("run-1")) == 1
    third = _durable(journal)
    third.restore(SESSION)
    assert third.auto_resolved_for("run-1") == []


def test_a_sweep_clears_the_journal_of_the_run_too(journal):
    store = _durable(journal)
    store.register("a", _entry("a", "run-1"))
    store.stash_auto_resolved("run-1", [_requirement("auto")])
    store.register("b", _entry("b", "run-2"))

    store.sweep_run("run-1")

    after = _durable(journal)
    assert after.restore(SESSION) == 1
    assert after.pending_ids() == ["b"]
    assert after.auto_resolved_for("run-1") == []


def test_a_run_that_was_being_continued_is_handed_back_once_after_a_restart(journal):
    before = _durable(journal)
    before.begin_continuing("run-7")

    after = _durable(journal)
    assert after.restore(SESSION) == 0
    assert after.take_interrupted_runs() == ["run-7"]
    assert after.take_interrupted_runs() == []

    # The marker went with it: a third backend finds nothing to continue.
    third = _durable(journal)
    third.restore(SESSION)
    assert third.take_interrupted_runs() == []


def test_a_run_whose_continuation_finished_leaves_no_marker(journal):
    before = _durable(journal)
    before.begin_continuing("run-7")
    before.end_continuing("run-7")

    after = _durable(journal)
    after.restore(SESSION)

    assert after.take_interrupted_runs() == []


def test_the_session_a_write_is_filed_under_is_asked_at_the_time_of_the_write(journal):
    current = ["s1"]
    store = PendingRequirementsStore(journal, session_id=lambda: current[0])
    store.register("a", _entry("a"))
    current[0] = "s2"
    store.register("b", _entry("b"))

    first, second = PendingRequirementsStore(journal), PendingRequirementsStore(journal)
    first.restore("s1")
    second.restore("s2")

    assert (first.pending_ids(), second.pending_ids()) == (["a"], ["b"])

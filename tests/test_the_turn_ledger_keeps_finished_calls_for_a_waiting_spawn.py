"""The turn ledger writes a run's finished calls to the journal once a spawn waits under it."""

from __future__ import annotations

from pathlib import Path

import pytest

from igni_core.core.approvals.journal import ApprovalJournal
from igni_core.core.hitl.turns import TurnLedger


@pytest.fixture
def journal(tmp_path: Path) -> ApprovalJournal:
    return ApprovalJournal(tmp_path / "state.db")


def test_without_a_journal_results_are_empty_and_nothing_raises():
    turns = TurnLedger()

    turns.tool_finished("r1", "c1", "out")
    turns.link("r1")
    turns.forget("r1")

    assert turns.results("r1") == {}


def test_a_call_finished_before_the_link_is_written_when_the_run_is_linked(journal):
    turns = TurnLedger()
    turns.attach_journal(journal)

    turns.tool_finished("r1", "c1", "first")
    assert turns.results("r1") == {}

    turns.link("r1")

    assert turns.results("r1") == {"c1": "first"}


def test_a_call_finished_after_the_link_is_written_at_once_and_an_unlinked_run_is_not(journal):
    turns = TurnLedger()
    turns.attach_journal(journal)
    turns.link("r1")

    turns.tool_finished("r1", "c2", "second")
    turns.tool_finished("r2", "c9", "other")

    assert turns.results("r1") == {"c2": "second"}
    assert turns.results("r2") == {}


def test_a_second_link_does_not_write_the_results_again(journal):
    turns = TurnLedger()
    turns.attach_journal(journal)
    turns.tool_finished("r1", "c1", "first")
    turns.link("r1")
    journal.turns.forget("r1")

    turns.link("r1")

    assert turns.results("r1") == {}


def test_forget_unlinks_the_run_and_drops_its_stored_and_remembered_results(journal):
    turns = TurnLedger()
    turns.attach_journal(journal)
    turns.tool_finished("r1", "c1", "first")
    turns.link("r1")

    turns.forget("r1")
    assert turns.results("r1") == {}

    # Unlinked and forgotten: a later link has nothing in memory to write, and a new call
    # is only remembered, not stored, until the run is linked again.
    turns.tool_finished("r1", "c2", "later")
    assert turns.results("r1") == {}
    turns.link("r1")
    assert turns.results("r1") == {"c2": "later"}


def test_forgetting_an_unknown_run_is_harmless(journal):
    turns = TurnLedger()
    turns.attach_journal(journal)

    turns.forget("never-seen")

    assert turns.results("never-seen") == {}


def test_the_same_call_finished_twice_keeps_the_last_result(journal):
    turns = TurnLedger()
    turns.attach_journal(journal)
    turns.link("r1")

    turns.tool_finished("r1", "c1", "old")
    turns.tool_finished("r1", "c1", "new")

    assert turns.results("r1") == {"c1": "new"}


def test_only_the_newest_runs_are_remembered_in_memory(journal):
    turns = TurnLedger()
    turns.attach_journal(journal)
    for index in range(TurnLedger.TURNS_KEPT + 1):
        turns.tool_finished(f"run-{index}", "c", f"result-{index}")

    turns.link("run-0")
    turns.link("run-1")

    assert turns.results("run-0") == {}
    assert turns.results("run-1") == {"c": "result-1"}

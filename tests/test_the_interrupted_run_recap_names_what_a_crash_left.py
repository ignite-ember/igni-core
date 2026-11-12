"""``InterruptedRunSummaryBuilder`` and its evidence, for what a crash leaves.

``test_interrupted_run_summary_builder`` pins the stamped path and one
locked Agno read; ``test_crash_survival`` pins the pending-only path. This
file pins the rest: the Agno run left at ``running`` (tools completed and
the partial reply), the wording for one question versus several, the
errored row's reason, the Agno and store read failures that must not block
startup, and that evidence is read-only.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from agno.run.base import RunStatus
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from igni_core.backend.schemas_lifecycle import AgnoRunSnapshot
from igni_core.backend.server_interrupted_run import (
    InterruptedRunEvidence,
    InterruptedRunSummaryBuilder,
)
from igni_core.core.session.pending_messages import (
    InterruptedMessage,
    PendingMessage,
    PendingMessageStore,
)

OPENING = "Previous run was interrupted before completion."


def _session(agno_session: object = None, *, error: Exception | None = None) -> MagicMock:
    session = MagicMock()
    session.session_id = "sess"
    session.main_team.aget_session = AsyncMock(return_value=agno_session, side_effect=error)
    return session


def _running(content: str = "", tools: tuple[str, ...] = (), status=RunStatus.running):  # type: ignore[no-untyped-def]
    run = SimpleNamespace(
        run_id="run-9",
        status=status,
        content=content,
        tools=[SimpleNamespace(tool_name=t) for t in tools],
    )
    return SimpleNamespace(runs=[run])


@pytest.fixture
def store(tmp_path: Path) -> PendingMessageStore:
    return PendingMessageStore(tmp_path / "state.db")


class TestAnAgnoRunLeftRunning:
    async def test_the_recap_names_the_tools_that_finished_and_the_partial_reply(
        self, store: PendingMessageStore
    ) -> None:
        pending = store.record_received("sess", "refactor the parser")
        agno = _running("Halfway through the pars", ("read_file", "grep"))

        summary = await InterruptedRunSummaryBuilder(_session(agno), store).build()

        assert summary is not None
        assert summary.recap == (
            f"{OPENING} The user had asked: 'refactor the parser'. "
            "Tool calls completed: read_file, grep. "
            "Partial response so far: 'Halfway through the pars'."
        )
        assert summary.pending_ids_to_drop == [pending]

    async def test_a_running_run_alone_is_still_an_interruption(
        self, store: PendingMessageStore
    ) -> None:
        summary = await InterruptedRunSummaryBuilder(_session(_running("partial")), store).build()

        assert summary is not None
        assert summary.recap == f"{OPENING} Partial response so far: 'partial'."
        assert summary.pending_ids_to_drop == []

    async def test_a_blank_preview_and_no_tools_add_nothing(
        self, store: PendingMessageStore
    ) -> None:
        store.record_received("sess", "hi")

        summary = await InterruptedRunSummaryBuilder(_session(_running("  \n")), store).build()

        assert summary is not None
        assert summary.recap == f"{OPENING} The user had asked: 'hi'."

    async def test_a_tool_with_no_name_is_a_question_mark(self, store: PendingMessageStore) -> None:
        agno = _running("x", ("",))

        summary = await InterruptedRunSummaryBuilder(_session(agno), store).build()

        assert summary is not None
        assert "Tool calls completed: ?." in summary.recap

    @pytest.mark.parametrize("status", [RunStatus.completed, RunStatus.error, None])
    async def test_a_last_run_that_is_not_running_is_no_evidence(
        self, store: PendingMessageStore, status: object
    ) -> None:
        agno = _running("done", ("grep",), status=status)  # type: ignore[arg-type]

        assert await InterruptedRunSummaryBuilder(_session(agno), store).build() is None

    @pytest.mark.parametrize("agno", [None, SimpleNamespace(runs=[]), SimpleNamespace(runs=None)])
    async def test_no_session_or_no_runs_is_no_evidence(
        self, store: PendingMessageStore, agno: object
    ) -> None:
        assert await InterruptedRunSummaryBuilder(_session(agno), store).build() is None

    async def test_only_the_last_run_counts(self, store: PendingMessageStore) -> None:
        earlier = _running("old", ("grep",)).runs[0]
        later = _running("fine", status=RunStatus.completed).runs[0]

        agno = SimpleNamespace(runs=[earlier, later])

        assert await InterruptedRunSummaryBuilder(_session(agno), store).build() is None

    async def test_stamped_rows_make_the_running_run_irrelevant(
        self, store: PendingMessageStore
    ) -> None:
        stamped = store.record_received("sess", "stamped one")
        store.mark_interrupted(stamped, "cancelled")
        session = _session(_running("ignored partial", ("grep",)))

        summary = await InterruptedRunSummaryBuilder(session, store).build()

        assert summary is not None
        assert summary.recap == f"{OPENING} The user had asked: 'stamped one'."
        assert "Partial" not in summary.recap


class TestSeveralQuestions:
    async def test_many_pending_messages_are_one_quoted_list(
        self, store: PendingMessageStore
    ) -> None:
        first = store.record_received("sess", "first?")
        second = store.record_received("sess", "second?")

        summary = await InterruptedRunSummaryBuilder(_session(), store).build()

        assert summary is not None
        assert summary.recap == f"{OPENING} The user had pending question(s): 'first?; second?'."
        assert summary.pending_ids_to_drop == [first, second]

    async def test_a_message_with_quotes_is_quoted_and_unicode_stays_readable(
        self, store: PendingMessageStore
    ) -> None:
        store.record_received("sess", 'what\'s "this" ünï?')

        summary = await InterruptedRunSummaryBuilder(_session(), store).build()

        assert summary is not None
        # Quoted as a literal, so a message that itself contains quotes
        # cannot be mistaken for the sentence around it; non-ASCII stays readable.
        assert summary.recap.endswith("""The user had asked: 'what\\'s "this" ünï?'.""")

    async def test_the_recap_carries_no_closing_instruction(
        self, store: PendingMessageStore
    ) -> None:
        store.record_received("sess", "hi")

        summary = await InterruptedRunSummaryBuilder(_session(), store).build()

        assert summary is not None
        assert summary.recap.endswith("'hi'.")


class TestTheReasonOfAnErroredRow:
    async def test_the_first_errored_row_with_a_message_gives_the_reason(
        self, store: PendingMessageStore
    ) -> None:
        cancelled = store.record_received("sess", "a")
        blank = store.record_received("sess", "b")
        errored = store.record_received("sess", "c")
        store.mark_interrupted(cancelled, "cancelled", last_error="not a reason")
        store.mark_interrupted(blank, "errored", last_error="")
        store.mark_interrupted(errored, "errored", last_error="quota exceeded")

        summary = await InterruptedRunSummaryBuilder(_session(), store).build()

        assert summary is not None
        assert summary.recap.endswith("Reason: 'quota exceeded'.")
        assert "not a reason" not in summary.recap
        assert "pending question(s)" in summary.recap

    def test_a_cancelled_row_has_no_reason(self) -> None:
        row = InterruptedMessage("m1", "sess", "q", 1, 2, "cancelled", "leftover")

        evidence = InterruptedRunEvidence.from_stamped([row])

        assert evidence.error is None
        assert "Reason" not in evidence.recap()
        assert evidence.stamped is True


class TestWhatABrokenReadDoesNotBlock:
    @pytest.mark.parametrize(
        "error",
        [
            ValueError("db not initialised"),
            json.JSONDecodeError("half a row", "{", 1),
            KeyError("runs"),
            TypeError("wrong shape"),
            OperationalError("SELECT", {}, Exception("database is locked")),
        ],
        ids=lambda e: type(e).__name__,
    )
    async def test_an_agno_read_failure_leaves_the_pending_rows_as_evidence(
        self, store: PendingMessageStore, error: Exception
    ) -> None:
        pending = store.record_received("sess", "still here")

        summary = await InterruptedRunSummaryBuilder(_session(error=error), store).build()

        assert summary is not None
        assert summary.pending_ids_to_drop == [pending]

    async def test_a_half_written_run_is_no_evidence_when_nothing_is_pending(
        self, store: PendingMessageStore
    ) -> None:
        error = ValidationError.from_exception_data("Run", [])

        assert await InterruptedRunSummaryBuilder(_session(error=error), store).build() is None

    async def test_an_error_of_another_kind_is_not_swallowed(
        self, store: PendingMessageStore
    ) -> None:
        with pytest.raises(RuntimeError):
            await InterruptedRunSummaryBuilder(_session(error=RuntimeError("bug")), store).build()

    async def test_a_failing_stamped_read_falls_through_to_the_pending_rows(self) -> None:
        pending_row = PendingMessage("p1", "sess", "pending q", 1)
        broken = OperationalError("SELECT", {}, Exception("locked"))
        store = MagicMock()
        store.alist_interrupted = AsyncMock(side_effect=broken)
        store.alist_pending = AsyncMock(return_value=[pending_row])

        summary = await InterruptedRunSummaryBuilder(_session(), store).build()

        assert summary is not None
        assert summary.pending_ids_to_drop == ["p1"]

    async def test_a_failing_pending_read_is_no_evidence(self) -> None:
        broken = OperationalError("SELECT", {}, Exception("locked"))
        store = MagicMock()
        store.alist_interrupted = AsyncMock(return_value=[])
        store.alist_pending = AsyncMock(side_effect=broken)

        assert await InterruptedRunSummaryBuilder(_session(), store).build() is None

    async def test_with_no_store_only_agno_can_testify(self) -> None:
        none_found = await InterruptedRunSummaryBuilder(_session(), None).build()
        running = await InterruptedRunSummaryBuilder(_session(_running("p")), None).build()

        assert none_found is None
        assert running is not None and running.pending_ids_to_drop == []


class TestTheEvidenceItself:
    def test_it_is_found_with_asked_rows_or_a_snapshot_only(self) -> None:
        asked = PendingMessage("m", "s", "q", 1)

        assert InterruptedRunEvidence().found is False
        assert InterruptedRunEvidence(error="boom").found is False
        assert InterruptedRunEvidence(asked=[asked]).found is True
        assert InterruptedRunEvidence(snapshot=AgnoRunSnapshot(run_id="r")).found is True

    def test_it_cannot_be_changed_once_built(self) -> None:
        evidence = InterruptedRunEvidence()

        with pytest.raises(ValidationError):
            evidence.error = "later"  # type: ignore[misc]

    def test_the_summary_drops_every_asked_message_and_says_nothing_to_do(self) -> None:
        evidence = InterruptedRunEvidence(
            asked=[PendingMessage("m1", "s", "a", 1), PendingMessage("m2", "s", "b", 2)]
        )

        summary = evidence.summary()

        assert summary.pending_ids_to_drop == ["m1", "m2"]
        assert summary.recap == evidence.recap()

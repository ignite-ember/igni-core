"""Session event-log unit tests.

Direct tests for :meth:`Session.append_event` and the
``get_chat_history`` splicing of ``role:"visualization"`` turns
from the event log. Real-DB round-trip tests live in
:file:`test_session_data_real_db.py` — this file uses
``Session.__new__`` (bypassing ``__init__``) so the reducer logic
can be exercised without spinning up the full Agno stack.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from igni_core.backend.schemas_history import (
    AssistantTurn,
    ChatTurn,
    ToolTurn,
    UserTurn,
)
from igni_core.backend.schemas_workflow_events import WorkflowEventRecord
from igni_core.backend.server_history import ChatHistoryRebuilder
from igni_core.core.session.core import Session
from igni_core.core.session.event_log import SessionEventLog
from igni_core.core.session.event_log_schema import (
    VisualizationDeltaDraft,
    VisualizationDeltaEntry,
    VisualizationDeltaPayload,
    WorkflowEventEntry,
)
from igni_core.core.session.schemas import PersistResult


def _bare_session() -> Session:
    """Build a Session without running ``__init__`` — we only
    need the event-log attributes for these tests."""
    sess = Session.__new__(Session)
    sess.persistence = None
    sess.event_log_store = SessionEventLog(persist_ref=lambda: sess.persistence)
    return sess


def _draft(spec_id: str = "s1", run_id: str = "r1", **payload: object) -> VisualizationDeltaDraft:
    """A viz-card draft — the kind a producer hands the log."""
    return VisualizationDeltaDraft(
        payload=VisualizationDeltaPayload(spec_id=spec_id, spec_json="{}", **payload),
        run_id=run_id,
    )


class TestAppendEvent:
    async def test_seq_monotonic(self):
        sess = _bare_session()
        await sess.append_event(_draft(spec_id="a"))
        await sess.append_event(_draft(spec_id="b"))
        await sess.append_event(_draft(spec_id="c", run_id="r2"))

        seqs = [e.seq for e in sess.event_log]
        assert seqs == [1, 2, 3]

    async def test_payload_is_copied_not_referenced(self):
        # Mutating the caller's card after append must not
        # bleed into the stored entry. Guarantees replay sees
        # exactly what was live at emission time.
        sess = _bare_session()
        draft = _draft(spec_id="s1")
        await sess.append_event(draft)
        draft.payload.spec_json = "MUTATED"
        assert sess.event_log[0].payload.spec_json == "{}"

    async def test_carries_run_id_and_type(self):
        sess = _bare_session()
        await sess.append_event(_draft(spec_id="s1", run_id="run-42"))
        entry = sess.event_log[0]
        assert entry.run_id == "run-42"
        assert entry.type == "visualization_delta"
        assert entry.payload.spec_id == "s1"

    async def test_calls_persistence_save_event_log(self):
        # Every append triggers persistence — the log is small and
        # ordering matters for replay, so we skip batching. Verified
        # via a spy on ``save_event_log``.
        sess = _bare_session()
        sess.persistence = MagicMock()
        sess.persistence.save_event_log = AsyncMock(return_value=PersistResult(ok=True))

        await sess.append_event(_draft(spec_id="x1"))
        await sess.append_event(_draft(spec_id="x2"))

        assert sess.persistence.save_event_log.await_count == 2
        # Last call sees BOTH entries — atomic-replace semantics.
        last_call = sess.persistence.save_event_log.await_args
        args = last_call.args if last_call else ()
        assert args and len(args[0]) == 2

    async def test_persistence_failure_does_not_swallow_in_memory(self):
        # A DB write failure must not lose the in-memory entry —
        # live listeners already got the event and reload-recovery
        # is the only sacrifice. The failure is REPORTED, not
        # swallowed: the caller gets a result that says so.
        sess = _bare_session()
        sess.persistence = MagicMock()
        sess.persistence.save_event_log = AsyncMock(side_effect=RuntimeError("db down"))

        result = await sess.append_event(_draft(spec_id="x1"))

        assert len(sess.event_log) == 1
        assert sess.event_log[0].payload.spec_id == "x1"
        assert result.persisted is False
        assert "db down" in (result.error or "")

    async def test_a_rejected_write_is_reported(self):
        # The store answers a failed write with ``ok=False`` rather
        # than raising — that answer must reach the caller too.
        sess = _bare_session()
        sess.persistence = MagicMock()
        sess.persistence.save_event_log = AsyncMock(
            return_value=PersistResult(ok=False, error="write failed")
        )

        result = await sess.append_event(_draft(spec_id="x1"))

        assert result.persisted is False
        assert result.error == "write failed"

    async def test_a_persisted_append_says_so(self):
        sess = _bare_session()
        sess.persistence = MagicMock()
        sess.persistence.save_event_log = AsyncMock(return_value=PersistResult(ok=True))

        result = await sess.append_event(_draft(spec_id="x1"))

        assert result.persisted is True
        assert result.error is None


class TestGetChatHistoryVizSplicing:
    """The BE-side splicing of viz cards from the event log into
    the chat history — replaces the reverted
    ``session.visualizations`` splicer with an event-log-driven
    one. Structural tests only; the real ``get_chat_history`` path
    is exercised elsewhere with real Agno sessions.
    """

    def _splice(self, history: list[ChatTurn], event_log: list[object]) -> list[ChatTurn]:
        """Run the real splicer over a fake session's log."""
        session = SimpleNamespace(event_log=list(event_log))
        out: list[ChatTurn] = list(history)
        ChatHistoryRebuilder(session)._splice_visualizations(out)  # type: ignore[arg-type]
        return out

    @staticmethod
    def _event(seq: int, run_id: str, spec_id: str, spec_json: str) -> VisualizationDeltaEntry:
        return VisualizationDeltaEntry(
            seq=seq,
            run_id=run_id,
            payload=VisualizationDeltaPayload(spec_id=spec_id, spec_json=spec_json),
        )

    def test_viz_inserted_after_matching_spawn_agent_tool_turn(self):
        history: list[ChatTurn] = [
            UserTurn(run_id="r1", content="hi"),
            ToolTurn(run_id="r1", tool_name="spawn_agent"),
            AssistantTurn(run_id="r1", content="here"),
        ]
        event_log = [self._event(1, "r1", "s1", '{"root":"r"}')]
        result = self._splice(history, event_log)
        # Viz card lands right AFTER the spawn_agent tool turn.
        assert [t.role for t in result] == [
            "user",
            "tool",
            "visualization",
            "assistant",
        ]
        viz = result[2]
        assert viz.spec_id == "s1"
        assert viz.spec == {"root": "r"}

    def test_nth_viz_pairs_with_nth_spawn_within_run(self):
        # Three spawn calls, three viz events — each viz should
        # land right after its own spawn. This is the "intermediate
        # messages between charts" bug the reverted patchwork
        # regressed on.
        history: list[ChatTurn] = [
            UserTurn(run_id="r1", content="hi"),
            ToolTurn(run_id="r1", tool_name="spawn_agent"),
            ToolTurn(run_id="r1", tool_name="spawn_agent"),
            ToolTurn(run_id="r1", tool_name="spawn_agent"),
            AssistantTurn(run_id="r1", content="here"),
        ]
        event_log = [self._event(i, "r1", f"s{i}", '{"root":"r"}') for i in (1, 2, 3)]
        result = self._splice(history, event_log)
        # Expect the shape: user, spawn, viz, spawn, viz, spawn, viz, assistant.
        assert [t.role for t in result] == [
            "user",
            "tool",
            "visualization",
            "tool",
            "visualization",
            "tool",
            "visualization",
            "assistant",
        ]
        # Pairing order preserved: viz #1 after spawn #1, etc.
        vis = [t for t in result if t.role == "visualization"]
        assert [v.spec_id for v in vis] == ["s1", "s2", "s3"]

    def test_viz_falls_back_to_last_turn_when_no_matching_spawn(self):
        # Historical run where the tool turn isn't reachable (e.g.
        # a synthetic run reconstructed from the summary). The card
        # still lands within its run, just at the tail.
        history: list[ChatTurn] = [
            UserTurn(run_id="r1", content="hi"),
            AssistantTurn(run_id="r1", content="here"),
        ]
        event_log = [self._event(1, "r1", "s1", '{"root":"r"}')]
        result = self._splice(history, event_log)
        # Viz appears after the last turn of the run.
        assert [t.role for t in result] == ["user", "assistant", "visualization"]

    def test_viz_with_unknown_run_id_appends_at_tail(self):
        # Belt-and-suspenders: an orphaned viz (run_id not in
        # history) doesn't disappear — appended at the very end
        # so the user still sees it after a session restore.
        history: list[ChatTurn] = [UserTurn(run_id="r1", content="hi")]
        event_log = [self._event(1, "ORPHAN", "s1", '{"root":"r"}')]
        result = self._splice(history, event_log)
        assert result[-1].role == "visualization"

    def test_malformed_json_payload_is_skipped(self):
        # A corrupt entry (truncated JSON) doesn't crash the
        # splicer — we drop it and keep going with the rest.
        history: list[ChatTurn] = [
            UserTurn(run_id="r1", content="hi"),
            ToolTurn(run_id="r1", tool_name="spawn_agent"),
        ]
        event_log = [
            self._event(1, "r1", "bad", '{"root": "r"'),  # truncated
            self._event(2, "r1", "good", '{"root":"r"}'),
        ]
        result = self._splice(history, event_log)
        vis = [t for t in result if t.role == "visualization"]
        assert len(vis) == 1
        assert vis[0].spec_id == "good"

    def test_a_card_that_is_not_json_is_skipped(self):
        # A card whose spec isn't JSON is not a spec — the splicer
        # drops it instead of raising.
        history: list[ChatTurn] = [
            UserTurn(run_id="r1", content="hi"),
            ToolTurn(run_id="r1", tool_name="spawn_agent"),
        ]
        event_log = [self._event(1, "r1", "bad", "not json")]
        result = self._splice(history, event_log)
        assert all(t.role != "visualization" for t in result)

    def test_non_viz_events_ignored(self):
        # The event log holds every kind the session records — a
        # workflow event MUST NOT accidentally get spliced as a viz
        # card.
        history: list[ChatTurn] = [
            UserTurn(run_id="r1", content="hi"),
            ToolTurn(run_id="r1", tool_name="spawn_agent"),
        ]
        event_log = [
            WorkflowEventEntry(
                seq=1,
                run_id="r1",
                payload=WorkflowEventRecord(
                    workflow_run_id="w1",
                    name="run",
                    ts=1,
                    seq=1,
                    type="workflow_started",
                    payload={},
                ),
            )
        ]
        result = self._splice(history, event_log)
        assert all(t.role != "visualization" for t in result)


class TestTheLogItself:
    """:class:`SessionEventLog` on its own: seeding, snapshots, rebinding."""

    @staticmethod
    def _entry(seq: int) -> VisualizationDeltaEntry:
        return VisualizationDeltaEntry(
            seq=seq, run_id="r", payload=VisualizationDeltaPayload(spec_id=f"s{seq}")
        )

    async def test_without_a_persister_the_event_is_kept_and_not_persisted(self):
        log = SessionEventLog()

        result = await log.append(_draft(spec_id="a"))

        assert result.persisted is False
        assert result.error is None
        assert [e.payload.spec_id for e in log.events] == ["a"]

    def test_restore_continues_after_the_highest_seq_on_disk(self):
        log = SessionEventLog()

        log.restore([self._entry(3), self._entry(7), self._entry(5)])

        assert log.seq == 7

    async def test_an_append_after_a_restore_does_not_collide(self):
        log = SessionEventLog()
        log.restore([self._entry(2), self._entry(4)])

        await log.append(_draft(spec_id="new"))

        assert [e.seq for e in log.events] == [2, 4, 5]

    def test_restoring_nothing_starts_again_at_zero(self):
        log = SessionEventLog()
        log.restore([self._entry(9)])

        log.restore([])

        assert log.events == []
        assert log.seq == 0

    async def test_restore_replaces_what_was_there(self):
        log = SessionEventLog()
        await log.append(_draft(spec_id="old"))

        log.restore([self._entry(1)])

        assert [e.payload.spec_id for e in log.events] == ["s1"]

    def test_restore_does_not_keep_the_callers_list(self):
        log = SessionEventLog()
        seed = [self._entry(1)]
        log.restore(seed)

        seed.append(self._entry(2))

        assert len(log.events) == 1

    async def test_the_events_are_a_snapshot_not_the_live_list(self):
        log = SessionEventLog()
        await log.append(_draft(spec_id="a"))

        log.events.clear()
        log.events.append(self._entry(99))

        assert [e.payload.spec_id for e in log.events] == ["a"]
        assert log.seq == 1

    async def test_a_rebound_persister_is_the_one_written_to(self):
        holder: dict[str, object] = {}
        first, second = MagicMock(), MagicMock()
        first.save_event_log = AsyncMock(return_value=PersistResult(ok=True))
        second.save_event_log = AsyncMock(return_value=PersistResult(ok=True))
        log = SessionEventLog(persist_ref=lambda: holder["p"])  # type: ignore[arg-type,return-value]

        holder["p"] = first
        await log.append(_draft(spec_id="a"))
        holder["p"] = second
        await log.append(_draft(spec_id="b"))

        assert first.save_event_log.await_count == 1
        assert second.save_event_log.await_count == 1

    async def test_a_failed_write_still_advances_the_seq(self):
        persistence = MagicMock()
        persistence.save_event_log = AsyncMock(side_effect=RuntimeError("down"))
        log = SessionEventLog(persist_ref=lambda: persistence)

        await log.append(_draft(spec_id="a"))
        await log.append(_draft(spec_id="b"))

        assert [e.seq for e in log.events] == [1, 2]

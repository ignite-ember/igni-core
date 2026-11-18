"""What the card drew, told back to the model.

The visualizer's spec goes to the client and never comes back, so the
only thing the model could say about a card was what it hoped for —
"Rendered a 6-element chart" over a card that drew nothing (campaign
R666).

This is the return path, and every part of it is pinned here:

* the book keeps a verdict against the card's spec id and revision, and
  a verdict about one draw cannot answer for another;
* the words the model reads say what the client drew, and nothing more;
* the visualizer's recap quotes that verdict, and falls back to the line
  it always said when nobody answers;
* the card's draw is numbered, so the client's answer names its draw;
* the client reports over one RPC, which lands in the book.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from types import SimpleNamespace
from typing import Any

import pytest
from agno.models.response import ToolExecution
from agno.run import agent as ev

from igni_core.backend.rpc_binding import RpcArgsError, RpcBinding
from igni_core.backend.rpc_handlers.panels import PanelRpcHandler
from igni_core.backend.schemas_visualization import VisualizationVerdictResult
from igni_core.core.tools.orchestrate_agent_stream import SubAgentStreamHandler
from igni_core.core.tools.orchestrate_events import VisualizationDeltaEvent
from igni_core.core.tools.orchestrate_registry import SubAgentRegistry
from igni_core.core.tools.orchestrate_stream_context import StreamContext, StreamSinks
from igni_core.core.tools.subagent_stream import SubAgentStreamState
from igni_core.core.tools.visualization_verdicts import (
    DEFAULT_WAIT_S,
    VERDICTS,
    ElementFailure,
    VerdictBook,
    VisualizationVerdict,
    verdict_wait_seconds,
)
from igni_core.protocol.rpc import RpcDuration, RpcMethod, RpcScope


def _verdict(
    spec_id: str = "card-1",
    revision: int = 1,
    total: int = 2,
    failed: tuple[ElementFailure, ...] = (),
    issues: tuple[str, ...] = (),
) -> VisualizationVerdict:
    return VisualizationVerdict(
        spec_id=spec_id,
        revision=revision,
        total=total,
        failed=failed,
        issues=issues,
    )


# ── the book ──────────────────────────────────────────────────────


class TestTheBook:
    def test_a_reported_verdict_is_read_back(self):
        book = VerdictBook()

        book.report(_verdict())

        assert book.get("card-1", 1) == _verdict()

    def test_a_card_nobody_reported_has_no_verdict(self):
        assert VerdictBook().get("card-1", 1) is None

    def test_a_verdict_about_one_draw_does_not_answer_for_another(self):
        book = VerdictBook()

        book.report(_verdict(revision=1))

        assert book.get("card-1", 2) is None

    def test_the_book_forgets_the_oldest_verdict_when_it_is_full(self):
        book = VerdictBook(max_entries=2)

        book.report(_verdict(spec_id="a"))
        book.report(_verdict(spec_id="b"))
        book.report(_verdict(spec_id="c"))

        assert book.get("a", 1) is None
        assert book.get("b", 1) is not None
        assert book.get("c", 1) is not None

    async def test_the_wait_returns_a_verdict_that_is_already_in_the_book(self):
        book = VerdictBook()
        book.report(_verdict())

        assert await book.await_verdict("card-1", 1, wait_s=0.0) == _verdict()

    async def test_the_wait_returns_the_verdict_that_lands_during_it(self):
        book = VerdictBook()

        async def report_soon() -> None:
            await asyncio.sleep(0.01)
            book.report(_verdict())

        waiter = asyncio.create_task(book.await_verdict("card-1", 1, wait_s=2.0))
        await report_soon()

        assert await waiter == _verdict()

    async def test_the_wait_gives_up_when_nobody_answers(self):
        book = VerdictBook()

        assert await book.await_verdict("card-1", 1, wait_s=0.01) is None

    async def test_a_verdict_that_lands_between_the_lookup_and_the_wait_is_still_read(self):
        """The report may land after the wait looked and before it began
        to wait — the waiter it would have signalled does not exist yet,
        so the verdict is found in the book rather than waited for."""

        class _LandingLookup(OrderedDict):
            """The book's own store, reporting a verdict the moment it is
            asked about one."""

            def __init__(self, book: VerdictBook) -> None:
                super().__init__()
                self._book = book
                self._armed = True

            def get(self, key: Any, default: Any = None) -> Any:
                if self._armed:
                    self._armed = False
                    self._book.report(_verdict())
                    return default
                return super().get(key, default)

        book = VerdictBook()
        book._verdicts = _LandingLookup(book)  # type: ignore[assignment]

        assert await book.await_verdict("card-1", 1, wait_s=0.05) == _verdict()

    async def test_the_wait_leaves_no_waiter_behind(self):
        book = VerdictBook()

        await book.await_verdict("card-1", 1, wait_s=0.01)

        assert book._waiters == {}


class TestTheWaitLength:
    def test_the_default_is_the_bounded_one(self, monkeypatch):
        monkeypatch.delenv("IGNI_VISUALIZATION_VERDICT_WAIT_S", raising=False)

        assert verdict_wait_seconds() == DEFAULT_WAIT_S

    def test_the_environment_sets_the_wait(self, monkeypatch):
        monkeypatch.setenv("IGNI_VISUALIZATION_VERDICT_WAIT_S", "0.25")

        assert verdict_wait_seconds() == 0.25

    @pytest.mark.parametrize("raw", ["", "  ", "not a number", "-1"])
    def test_a_wait_that_makes_no_sense_falls_back_to_the_default(self, raw, monkeypatch):
        monkeypatch.setenv("IGNI_VISUALIZATION_VERDICT_WAIT_S", raw)

        assert verdict_wait_seconds() == DEFAULT_WAIT_S


# ── the words the model reads ─────────────────────────────────────


class TestTheVerdictInWords:
    def test_a_card_with_nothing_in_it_drew_nothing(self):
        assert _verdict(total=0).said() == "The card drew nothing."

    def test_a_card_that_drew_everything_says_so(self):
        assert _verdict(total=3).said() == "The card drew all 3 elements."

    def test_a_card_that_drew_part_of_itself_says_how_much(self):
        verdict = _verdict(total=3, failed=(ElementFailure(type="BarGraph", message="no data"),))

        assert verdict.said() == (
            "The card drew 2 of 3 elements. It could not draw BarGraph: no data"
        )

    def test_an_element_that_failed_without_a_reason_is_still_named(self):
        verdict = _verdict(total=1, failed=(ElementFailure(type="LineChart"),))

        assert verdict.said() == "The card drew 0 of 1 elements. It could not draw LineChart."

    def test_what_was_wrong_with_the_shape_is_said_too(self):
        verdict = _verdict(total=1, issues=("a child is not an element of it",))

        assert verdict.said() == (
            "The card drew all 1 elements. It could not be fully drawn: "
            "a child is not an element of it"
        )

    def test_the_words_never_promise_a_rendering(self):
        verdict = _verdict(total=1, failed=(ElementFailure(type="BarGraph"),))

        assert "render" not in verdict.said().lower()
        assert "ready" not in verdict.said().lower()

    def test_how_much_was_drawn_is_the_difference_between_the_two_counts(self):
        assert VisualizationVerdict(spec_id="a", revision=1, total=2).drawn == 2
        assert (
            VisualizationVerdict(
                spec_id="a",
                revision=1,
                total=2,
                failed=(ElementFailure(type="x"), ElementFailure(type="y")),
            ).drawn
            == 0
        )


# ── the draw is numbered ──────────────────────────────────────────


class TestTheRevision:
    def test_the_first_draw_is_one_and_the_next_is_two(self):
        state = SubAgentStreamState(agent_path_id="root")

        assert state.next_vis_revision() == 1
        assert state.next_vis_revision() == 2

    def test_the_delta_carries_the_draw_it_is_about(self):
        event = VisualizationDeltaEvent(
            agent_path="root", spec_id="card-1", spec_json="{}", final=True, revision=2
        )

        assert event.revision == 2

    def test_a_partial_frame_carries_no_revision(self):
        event = VisualizationDeltaEvent(agent_path="root", spec_id="card-1", spec_json="{}")

        assert event.revision is None

    def test_a_partial_frame_leaves_the_revision_off_the_wire(self):
        """The wire drops a null field, so a frame that is not a draw
        does not claim to be one."""
        payload = VisualizationDeltaEvent(
            agent_path="root", spec_id="card-1", spec_json="{}"
        ).model_dump(by_alias=True, exclude_none=True)

        assert "revision" not in payload


# ── the recap the parent agent reads ──────────────────────────────


class _Runnable:
    """Plays one scripted tape."""

    def __init__(self, tape: list[Any]) -> None:
        self.tape = tape

    def arun(self, task: str, *, stream: bool = True, stream_events: bool = False) -> Any:
        return self._play(self.tape)

    @staticmethod
    async def _play(tape: list[Any]):
        for event in tape:
            yield event

    async def aget_run_output(self, *, run_id: str, session_id: str) -> Any:
        return None

    async def aget_last_run_output(self, *, session_id: str) -> Any:
        return None


class _Sinks:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def on_progress(self, event: Any) -> None:
        self.events.append(event)

    def of(self, kind: type) -> list[Any]:
        return [e for e in self.events if isinstance(e, kind)]


def _tool(name: str = "t", args: Any = None, call_id: str | None = "c1") -> Any:
    return ToolExecution(
        tool_name=name, tool_args=args if args is not None else {}, tool_call_id=call_id
    )


def _handler(tape: list[Any], *, path: list[str] | None = None):
    sinks = _Sinks()
    context = StreamContext(
        runnable=_Runnable(tape),
        task="do it",
        agent_path=path or [],
        card_id="card",
        sinks=StreamSinks(
            on_progress=sinks.on_progress,
            event_appender=None,
            hitl_coordinator=None,
            subagent_registry=SubAgentRegistry(),
        ),
    )
    return SubAgentStreamHandler(context), sinks


def _visualize_tape() -> list[Any]:
    return [
        ev.ToolCallStartedEvent(tool=_tool("visualize", {"spec": {"root": "r"}})),
        ev.RunCompletedEvent(content="a chart"),
    ]


class TestTheRecap:
    async def test_the_recap_quotes_the_verdict_the_client_gave(self):
        handler, _ = _handler(_visualize_tape(), path=["main", "visualizer"])
        VERDICTS.report(
            VisualizationVerdict(
                spec_id=handler.state.vis_spec_id,
                revision=1,
                total=2,
                failed=(ElementFailure(type="BarGraph", message="no data"),),
            )
        )

        final, _log = await handler.run()

        assert final == (
            "Emitted visualization to the client. The card drew 1 of 2 elements. "
            "It could not draw BarGraph: no data"
        )

    async def test_without_a_verdict_the_recap_is_the_line_it_always_was(self, monkeypatch):
        monkeypatch.setenv("IGNI_VISUALIZATION_VERDICT_WAIT_S", "0")
        handler, _ = _handler(_visualize_tape(), path=["main", "visualizer"])

        final, _log = await handler.run()

        assert final == "Emitted visualization to the client."

    async def test_a_card_that_drew_nothing_is_said_as_nothing(self):
        handler, _ = _handler(_visualize_tape(), path=["main", "visualizer"])
        VERDICTS.report(
            VisualizationVerdict(spec_id=handler.state.vis_spec_id, revision=1, total=0)
        )

        final, _log = await handler.run()

        assert final == "Emitted visualization to the client. The card drew nothing."

    async def test_an_agent_that_is_not_the_visualizer_says_its_own_answer(self):
        handler, _ = _handler([ev.RunCompletedEvent(content="the answer")], path=["main", "editor"])

        final, _log = await handler.run()

        assert final == "the answer"

    async def test_the_draw_the_delta_carries_is_the_one_the_verdict_names(self):
        handler, sinks = _handler(
            [
                ev.ToolCallStartedEvent(tool=_tool("visualize", {"spec": {"root": "r"}})),
                ev.ToolCallStartedEvent(tool=_tool("visualize", {"spec": {"root": "s"}})),
            ],
            path=["main", "visualizer"],
        )

        await handler.run()

        assert [delta.revision for delta in sinks.of(VisualizationDeltaEvent)] == [1, 2]


# ── the report over the wire ──────────────────────────────────────


class _Backend:
    """What the panel calls ask of the session's backend, recorded."""

    def __init__(self) -> None:
        self.asked: list[VisualizationVerdict] = []

    def report_visualization_verdict(self, verdict: VisualizationVerdict) -> Any:
        self.asked.append(verdict)
        return VisualizationVerdictResult.of(verdict)


def _calls(backend: _Backend) -> dict[RpcMethod, Any]:
    ctx = SimpleNamespace(backend=backend)
    return PanelRpcHandler(ctx).methods()  # type: ignore[arg-type]


class TestTheReportOverTheWire:
    def test_the_call_is_registered_and_declares_its_scope_and_duration(self):
        bindings = RpcBinding.on(PanelRpcHandler)

        assert RpcMethod.REPORT_VISUALIZATION_VERDICT in bindings
        binding = bindings[RpcMethod.REPORT_VISUALIZATION_VERDICT][1]
        assert binding.scope is RpcScope.SESSION
        assert binding.duration is RpcDuration.INSTANT

    def test_the_call_reaches_the_backend_with_the_verdict_it_was_given(self):
        backend = _Backend()

        result = _calls(backend)[RpcMethod.REPORT_VISUALIZATION_VERDICT](
            {
                "spec_id": "card-1",
                "revision": 2,
                "total": 3,
                "failed": [{"type": "BarGraph", "message": "no data"}],
                "issues": ["a child is not an element of it"],
            }
        )

        (verdict,) = backend.asked
        assert verdict.spec_id == "card-1"
        assert verdict.revision == 2
        assert verdict.total == 3
        assert verdict.failed == (ElementFailure(type="BarGraph", message="no data"),)
        assert verdict.issues == ("a child is not an element of it",)
        assert result == VisualizationVerdictResult(
            ok=True, spec_id="card-1", revision=2, drawn=2, total=3
        )

    def test_a_call_without_a_card_is_refused(self):
        with pytest.raises(RpcArgsError, match="'spec_id' is required"):
            _calls(_Backend())[RpcMethod.REPORT_VISUALIZATION_VERDICT]({})

    def test_a_key_the_call_does_not_name_is_ignored(self):
        backend = _Backend()

        _calls(backend)[RpcMethod.REPORT_VISUALIZATION_VERDICT](
            {"spec_id": "card-1", "something_new": 1}
        )

        assert backend.asked[0].spec_id == "card-1"

    def test_a_negative_draw_is_refused(self):
        with pytest.raises(RpcArgsError):
            _calls(_Backend())[RpcMethod.REPORT_VISUALIZATION_VERDICT](
                {"spec_id": "card-1", "revision": -1}
            )

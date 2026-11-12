"""``RunController``'s run, from the submit to the Stop hook, through its public surface.

A run is the gate, the UserPromptSubmit hook, the prompt, the guardrail
caveat, the stream, the books kept on the way and the Stop hook. These tests
drive ``run_message`` and friends over a rig of recording collaborators and
read what the person's window receives, what the model is handed, and what
the books say afterwards.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from agno.agent import Agent
from agno.media import Image

from igni_core.backend.run_controller import RunCancelledError, RunController
from igni_core.backend.schemas_lifecycle import InterruptedRunSummary
from igni_core.backend.schemas_run import HookGateResult, PromptBuildResult, RunPhase
from igni_core.core.config.group_policy.gate import RunGate
from igni_core.core.utils.media_schemas import MediaAttachments
from igni_core.protocol import messages as msg


class _Journal:
    def __init__(self) -> None:
        self.events: list[tuple[str, ...]] = []
        self.queued: list[str] = []

    def queue_drops(self, ids: list[str]) -> None:
        self.queued.extend(ids)

    async def drain_queued_drops(self) -> None:
        self.events.append(("drain",))

    async def record(self, text: str) -> str:
        self.events.append(("record", text))
        return "pending-0123456789"

    async def mark_completed(self, pending_id: str) -> None:
        self.events.append(("completed", pending_id))

    async def mark_interrupted(
        self, pending_id: str, reason: str, last_error: str | None = None
    ) -> None:
        self.events.append(("interrupted", pending_id, reason, last_error or ""))


class _Hooks:
    def __init__(self) -> None:
        self.submit = HookGateResult(should_continue=True)
        self.stop = HookGateResult(should_continue=True)
        self.submitted: list[str] = []
        self.stopped = 0

    async def fire_user_prompt_submit(self, text: str) -> HookGateResult:
        self.submitted.append(text)
        if self.submit.should_continue and self.submit.text is None:
            return self.submit.model_copy(update={"text": text})
        return self.submit

    async def fire_stop(self) -> HookGateResult:
        self.stopped += 1
        return self.stop


class _Builder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, str | None]] = []
        self.infos: list[msg.Info] = []

    async def build(self, text, media, *, interrupted_summary):
        self.calls.append((text, media, interrupted_summary))
        return PromptBuildResult(message=f"<built>{text}", media=media, info_messages=self.infos)


class _Rig:
    """One controller and everything it was handed, recorded."""

    def __init__(self, stream: list[msg.Message] | BaseException | None = None) -> None:
        self.journal = _Journal()
        self.hooks = _Hooks()
        self.builder = _Builder()
        self.stream_script = stream if stream is not None else [msg.StreamingDone(run_id="r1")]
        self.arun_calls: list[tuple[str, dict]] = []
        self.order: list[str] = []
        self.prefix = ""
        self.settled: list[Any] = []
        self.mirror_syncs = 0
        self.closed: list[Any] = []
        self.foreground_kills = 0
        self.handled: list[msg.Message] = []

        rig = self

        async def _stream(team_stream):
            rig.order.append("stream")
            if isinstance(rig.stream_script, BaseException):
                raise rig.stream_script
            if rig.stream_script == "block":
                await asyncio.sleep(3600)
            for message in rig.stream_script:
                yield message

        async def _warning_prefix(text: str) -> str:
            rig.order.append("guardrails")
            return rig.prefix

        async def _close_and_replace(team) -> None:
            rig.closed.append(team)

        async def _forever(team, *a, **k) -> None:
            await asyncio.sleep(3600)

        class _Queue:
            async def settle(self, db) -> None:
                rig.settled.append(db)

        class _Mirror:
            def sync(self) -> None:
                rig.mirror_syncs += 1

        class _Dispatcher:
            async def handle(self, proto, team) -> None:
                rig.handled.append(proto)

        def _arun(message, stream=True, **kwargs):
            rig.arun_calls.append((message, kwargs))

        self.team = SimpleNamespace(arun=_arun)
        c = RunController.__new__(RunController)
        c._phase = RunPhase.idle
        c._interrupted_summary = None
        c._pending_id_for_cancel = None
        c._current_run_task = None
        c._cancelled_by_person = None
        c._run_lock = asyncio.Lock()
        c._pending_journal = self.journal
        c._hook_gate = self.hooks
        c._dispatcher = _Dispatcher()
        c._prompt_builder = self.builder
        c._session = SimpleNamespace(
            main_team=self.team,
            guardrail_runner=SimpleNamespace(warning_prefix=_warning_prefix),
            memory_mirror=_Mirror(),
            db="the-db",
        )
        c._stream_factory = _stream
        c._message_queue = _Queue()
        c._http_client_manager = SimpleNamespace(close_and_replace=_close_and_replace)
        c._supervisor = SimpleNamespace(cancel_foreground=self._kill)
        c._gate = RunGate()
        c.periodic_checkpoint = _forever  # type: ignore[method-assign]
        self.controller = c

    def _kill(self) -> bool:
        self.foreground_kills += 1
        return True

    async def run(self, text: str = "hello", media: Any = None) -> list[msg.Message]:
        return [m async for m in self.controller.run_message(text, media)]


async def _until(predicate) -> None:
    async def _spin() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_spin(), timeout=5)


# ── The run gate ─────────────────────────────────────────────────────


class TestAClosedGateRefusesTheRun:
    async def test_the_person_is_told_the_gate_s_reason_and_nothing_runs(self):
        rig = _Rig()
        rig.controller._gate.close("Sign in to fetch your organisation's policy pack.")

        got = await rig.run()

        assert [type(m) for m in got] == [msg.Error]
        assert got[0].text == "Sign in to fetch your organisation's policy pack."
        assert rig.controller.phase is RunPhase.done
        assert rig.arun_calls == []
        assert rig.builder.calls == []
        assert rig.hooks.submitted == []
        assert not [e for e in rig.journal.events if e[0] == "record"]

    async def test_an_open_gate_lets_the_run_start(self):
        rig = _Rig()

        got = await rig.run()

        assert [type(m) for m in got] == [msg.StreamingDone]
        assert len(rig.arun_calls) == 1


# ── UserPromptSubmit ─────────────────────────────────────────────────


class TestTheUserPromptSubmitHook:
    async def test_a_block_is_an_error_with_the_hook_s_message_and_the_run_ends_done(self):
        rig = _Rig()
        rig.hooks.submit = HookGateResult(should_continue=False, block_message="No secrets.")

        got = await rig.run("pw=1")

        assert [(type(m), m.text) for m in got] == [(msg.Error, "No secrets.")]
        assert rig.controller.phase is RunPhase.done
        assert rig.arun_calls == [] and rig.builder.calls == []
        assert rig.hooks.stopped == 0

    async def test_a_block_without_a_message_says_blocked_by_hook(self):
        rig = _Rig()
        rig.hooks.submit = HookGateResult(should_continue=False)

        got = await rig.run()

        assert got[0].text == "Message blocked by hook."

    async def test_a_text_an_extension_rewrote_is_what_the_prompt_is_built_from(self):
        rig = _Rig()
        rig.hooks.submit = HookGateResult(should_continue=True, text="login pw=…")

        await rig.run("login pw=1")

        assert rig.builder.calls[0][0] == "login pw=…"
        assert ("record", "login pw=…") in rig.journal.events

    async def test_the_hook_s_context_is_appended_to_the_prompt_in_a_block(self):
        rig = _Rig()
        rig.hooks.submit = HookGateResult(should_continue=True, context_message="use tabs")

        await rig.run("hi")

        assert rig.arun_calls[0][0] == "<built>hi\n<hook-context>use tabs</hook-context>"

    async def test_no_context_leaves_the_prompt_alone(self):
        rig = _Rig()

        await rig.run("hi")

        assert rig.arun_calls[0][0] == "<built>hi"


# ── The prompt ───────────────────────────────────────────────────────


class TestThePromptTheModelIsHanded:
    async def test_the_prompt_builder_s_announcements_precede_the_stream(self):
        rig = _Rig([msg.ContentDelta(text="x")])
        rig.builder.infos = [msg.Info(text="Referenced: a.py")]

        got = await rig.run()

        assert [(type(m), getattr(m, "text", None)) for m in got] == [
            (msg.Info, "Referenced: a.py"),
            (msg.ContentDelta, "x"),
        ]

    async def test_a_guardrail_warning_is_prepended_to_what_the_model_is_sent(self):
        rig = _Rig()
        rig.prefix = "[GUARDRAIL WARNING] pii\n"

        await rig.run("my ssn")

        assert rig.arun_calls[0][0] == "[GUARDRAIL WARNING] pii\n<built>my ssn"

    async def test_the_guardrails_check_the_user_s_text_before_the_model_is_called(self):
        rig = _Rig()

        await rig.run("x")

        assert rig.order == ["guardrails", "stream"]

    async def test_the_guardrail_warning_goes_before_the_hook_context(self):
        rig = _Rig()
        rig.prefix = "[W] "
        rig.hooks.submit = HookGateResult(should_continue=True, context_message="ctx")

        await rig.run("hi")

        assert rig.arun_calls[0][0] == "[W] <built>hi\n<hook-context>ctx</hook-context>"

    async def test_no_media_passes_no_media_kwargs(self):
        rig = _Rig()

        await rig.run("hi", None)

        assert rig.arun_calls[0][1] == {}

    async def test_media_is_passed_as_the_model_call_s_kwargs(self):
        rig = _Rig()
        image = Image(filepath=Path("/project/a.png"))

        await rig.run("hi", MediaAttachments(images=[image]))

        assert rig.arun_calls[0][1] == {"images": [image]}


class TestTheInterruptedRunNote:
    async def test_it_rides_along_with_the_next_message_once(self):
        rig = _Rig()
        rig.controller.set_interrupted_summary(
            InterruptedRunSummary(recap="You were editing a.py.")
        )

        await rig.run("go on")
        await rig.run("again")

        first, second = rig.builder.calls[0][2], rig.builder.calls[1][2]
        assert first is not None and "You were editing a.py." in first
        assert InterruptedRunSummary.WITH_USER_MESSAGE in first
        assert second is None

    async def test_a_blank_message_gets_the_awaiting_user_ending(self):
        rig = _Rig()
        rig.controller.set_interrupted_summary(InterruptedRunSummary(recap="You were editing."))

        await rig.run("   ")

        note = rig.builder.calls[0][2]
        assert note is not None and InterruptedRunSummary.AWAITING_USER in note

    async def test_a_clean_shutdown_has_no_note(self):
        rig = _Rig()

        await rig.run("hi")

        assert rig.builder.calls[0][2] is None

    def test_the_pending_rows_to_drop_are_queued_with_the_summary(self):
        rig = _Rig()

        rig.controller.set_interrupted_summary(
            InterruptedRunSummary(recap="r", pending_ids_to_drop=["p1", "p2"])
        )

        assert rig.journal.queued == ["p1", "p2"]

    def test_clearing_the_summary_queues_nothing(self):
        rig = _Rig()

        rig.controller.set_interrupted_summary(None)

        assert rig.journal.queued == []

    async def test_the_queued_drops_are_drained_before_the_message_is_recorded(self):
        rig = _Rig()

        await rig.run("hi")

        kinds = [e[0] for e in rig.journal.events]
        assert kinds.index("drain") < kinds.index("record")


# ── The stream and its books ─────────────────────────────────────────


class TestTheBooksOfARun:
    async def test_every_streamed_message_reaches_the_window_and_the_dispatcher(self):
        stream = [msg.ContentDelta(text="a"), msg.ContentDelta(text="b"), msg.StreamingDone()]
        rig = _Rig(list(stream))

        got = await rig.run()

        assert got == stream
        assert rig.handled == stream

    async def test_a_clean_run_marks_the_pending_message_completed_and_fires_stop(self):
        rig = _Rig()

        await rig.run("hi")

        assert rig.journal.events[-1] == ("completed", "pending-0123456789")
        assert rig.hooks.stopped == 1
        assert rig.controller.phase is RunPhase.done
        assert rig.controller._pending_id_for_cancel is None

    async def test_a_stop_hook_that_blocks_has_its_message_shown_as_info_after_the_run(self):
        rig = _Rig([msg.ContentDelta(text="x")])
        rig.hooks.stop = HookGateResult(should_continue=False, block_message="Run the tests.")

        got = await rig.run()

        assert [(type(m), m.text) for m in got] == [
            (msg.ContentDelta, "x"),
            (msg.Info, "Run the tests."),
        ]
        assert rig.controller.phase is RunPhase.done

    async def test_a_stop_hook_that_blocks_without_a_message_says_nothing(self):
        rig = _Rig([msg.ContentDelta(text="x")])
        rig.hooks.stop = HookGateResult(should_continue=False)

        got = await rig.run()

        assert [type(m) for m in got] == [msg.ContentDelta]

    async def test_a_failed_stream_does_not_fire_stop_or_complete_the_message(self):
        rig = _Rig([msg.Error(text="model connection reset")])

        await rig.run()

        assert rig.hooks.stopped == 0
        assert ("completed", "pending-0123456789") not in rig.journal.events

    async def test_an_error_with_no_text_is_recorded_as_run_failed(self):
        rig = _Rig([msg.Error(text="")])

        await rig.run()

        assert ("interrupted", "pending-0123456789", "errored", "run failed") in rig.journal.events

    async def test_a_run_error_with_no_text_is_recorded_as_run_failed(self):
        rig = _Rig([msg.RunError(error="")])

        await rig.run()

        assert ("interrupted", "pending-0123456789", "errored", "run failed") in rig.journal.events

    async def test_an_exception_mid_run_is_recorded_then_re_raised_with_the_phase_errored(self):
        rig = _Rig(RuntimeError("boom"))

        with pytest.raises(RuntimeError, match="boom"):
            await rig.run()

        assert ("interrupted", "pending-0123456789", "errored", "boom") in rig.journal.events
        assert rig.controller.phase is RunPhase.errored
        assert rig.controller._pending_id_for_cancel is None
        assert rig.hooks.stopped == 0


class TestTheTeardownOfARun:
    async def test_a_clean_run_settles_the_queue_syncs_memory_and_renews_the_client(self):
        rig = _Rig()

        await rig.run()

        assert rig.settled == ["the-db"]
        assert rig.mirror_syncs == 1
        assert rig.closed == [rig.team]

    async def test_a_failed_run_tears_down_too(self):
        rig = _Rig(RuntimeError("boom"))

        with pytest.raises(RuntimeError):
            await rig.run()

        assert (rig.settled, rig.mirror_syncs, rig.closed) == (["the-db"], 1, [rig.team])

    async def test_without_a_queue_or_a_mirror_the_run_still_ends(self):
        rig = _Rig()
        rig.controller._message_queue = None
        rig.controller._session.memory_mirror = None

        got = await rig.run()

        assert [type(m) for m in got] == [msg.StreamingDone]
        assert rig.controller.phase is RunPhase.done

    async def test_a_hook_blocked_run_never_reaches_the_teardown(self):
        rig = _Rig()
        rig.hooks.submit = HookGateResult(should_continue=False, block_message="no")

        await rig.run()

        assert (rig.settled, rig.mirror_syncs, rig.closed) == ([], 0, [])


# ── Cancelling ───────────────────────────────────────────────────────


class TestCancellingARun:
    async def _streaming(self, rig: _Rig) -> asyncio.Task:
        rig.stream_script = "block"  # type: ignore[assignment]
        got: list[msg.Message] = []
        rig.got = got  # type: ignore[attr-defined]

        async def consume() -> None:
            async for m in rig.controller.run_message("hello", None):
                got.append(m)

        task = asyncio.create_task(consume())
        await _until(lambda: rig.controller.phase is RunPhase.streaming)
        return task

    async def test_the_person_is_told_the_run_was_cancelled(self):
        rig = _Rig()
        task = await self._streaming(rig)

        assert rig.controller.cancel_run() is True
        await asyncio.wait_for(task, 2)

        assert [(type(m), m.text) for m in rig.got] == [(msg.Info, "Run cancelled by user.")]  # type: ignore[attr-defined]

    async def test_cancel_stamps_the_pending_message_cancelled_and_kills_the_foreground_shell(
        self,
    ):
        rig = _Rig()
        task = await self._streaming(rig)

        rig.controller.cancel_run()
        await asyncio.wait_for(task, 2)
        await _until(
            lambda: ("interrupted", "pending-0123456789", "cancelled", "") in rig.journal.events
        )

        assert rig.foreground_kills == 1

    async def test_cancelling_the_run_does_not_ask_agno_to_cancel_a_team_run_id(self, monkeypatch):
        """``Agent.cancel_run(team.run_id)`` never fired — the team has no ``run_id`` — and
        the task's own cancellation is what stops the run."""

        def _must_not_be_called(run_id: str) -> bool:
            raise AssertionError("cancel_run went through Agent.cancel_run")

        monkeypatch.setattr(Agent, "cancel_run", _must_not_be_called)
        rig = _Rig()
        task = await self._streaming(rig)

        assert rig.controller.cancel_run() is True
        await asyncio.wait_for(task, 2)

        assert rig.controller.phase is RunPhase.done

    async def test_cancelling_an_idle_session_answers_false_and_changes_nothing(self):
        rig = _Rig()

        assert rig.controller.cancel_run() is False

        assert rig.journal.events == []
        assert rig.foreground_kills == 0
        assert rig.controller.phase is RunPhase.idle

    async def test_a_cancel_after_the_run_finished_is_a_no_op(self):
        rig = _Rig()
        await rig.run()
        events = list(rig.journal.events)

        assert rig.controller.cancel_run() is False
        await asyncio.sleep(0.05)

        assert rig.journal.events == events

    async def test_a_cancelled_run_leaves_the_lock_for_the_next_submit(self):
        rig = _Rig()
        task = await self._streaming(rig)
        rig.controller.cancel_run()
        await asyncio.wait_for(task, 2)
        rig.stream_script = [msg.StreamingDone()]

        got = await asyncio.wait_for(rig.run(), 2)

        assert [type(m) for m in got] == [msg.StreamingDone]


class TestTwoSubmitsAreOneAtATime:
    async def test_the_second_waits_for_the_first_to_end(self):
        rig = _Rig()
        first_open = asyncio.Event()
        release = asyncio.Event()
        order: list[str] = []

        async def _stream(_team_stream):
            order.append("start")
            first_open.set()
            await release.wait()
            order.append("end")
            yield msg.StreamingDone()

        rig.controller._stream_factory = _stream

        t1 = asyncio.create_task(rig.run("one"))
        await first_open.wait()
        t2 = asyncio.create_task(rig.run("two"))
        await asyncio.sleep(0.05)
        assert order == ["start"]
        assert rig.controller.run_lock.locked()

        release.set()
        await asyncio.gather(t1, t2)

        assert order == ["start", "end", "start", "end"]


class TestUnstreamedWork:
    async def test_work_a_person_does_not_cancel_propagates_its_own_cancellation(self):
        rig = _Rig()
        started = asyncio.Event()

        async def work() -> str:
            started.set()
            await asyncio.sleep(3600)
            return "never"

        running = asyncio.create_task(rig.controller.run_unstreamed(work))
        await started.wait()
        running.cancel()

        with pytest.raises(asyncio.CancelledError):
            await running
        assert not rig.controller.phase.is_active()
        assert rig.controller.current_run_task is None

    async def test_a_persons_cancel_is_a_run_cancelled_error(self):
        rig = _Rig()
        started = asyncio.Event()

        async def work() -> str:
            started.set()
            await asyncio.sleep(3600)
            return "never"

        running = asyncio.create_task(rig.controller.run_unstreamed(work))
        await started.wait()
        rig.controller.cancel_run()

        with pytest.raises(RunCancelledError, match="cancelled by user"):
            await running

    async def test_the_result_of_the_work_is_returned_and_the_phase_ends_done(self):
        rig = _Rig()

        async def work() -> int:
            return 7

        assert await rig.controller.run_unstreamed(work) == 7
        assert rig.controller.phase is RunPhase.done


# ── cancel_agent_run ─────────────────────────────────────────────────


class TestCancellingASubAgentRun:
    def test_an_empty_id_is_refused(self):
        result = _Rig().controller.cancel_agent_run("")

        assert (result.ok, result.error) == (False, "missing run_id")

    def test_a_run_that_is_not_running_is_reported(self, monkeypatch):
        monkeypatch.setattr(Agent, "cancel_run", lambda run_id: False)

        result = _Rig().controller.cancel_agent_run("r-9")

        assert (result.ok, result.error) == (False, "no run 'r-9' is running")

    def test_a_live_run_is_cancelled(self, monkeypatch):
        seen: list[str] = []
        monkeypatch.setattr(Agent, "cancel_run", lambda run_id: seen.append(run_id) or True)

        result = _Rig().controller.cancel_agent_run("r-9")

        assert result.ok is True and not result.error
        assert seen == ["r-9"]

    def test_agno_raising_is_reported_not_raised(self, monkeypatch):
        def _boom(run_id: str) -> bool:
            raise RuntimeError("agno is unhappy")

        monkeypatch.setattr(Agent, "cancel_run", _boom)

        result = _Rig().controller.cancel_agent_run("r-9")

        assert (result.ok, result.error) == (False, "agno is unhappy")


# ── State and checkpoints ────────────────────────────────────────────


class TestTheStateItShows:
    def test_idle_is_not_processing(self):
        c = _Rig().controller

        assert (c.is_processing(), c.phase, c.current_run_task) == (False, RunPhase.idle, None)

    def test_it_exposes_its_journal_and_lock(self):
        rig = _Rig()

        assert rig.controller.pending_journal is rig.journal
        assert isinstance(rig.controller.run_lock, asyncio.Lock)

    async def test_a_streaming_run_is_processing(self):
        rig = _Rig("block")  # type: ignore[arg-type]
        task = asyncio.create_task(rig.run())
        await _until(lambda: rig.controller.phase is RunPhase.streaming)

        assert rig.controller.is_processing() is True

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class TestCheckpoints:
    async def test_a_checkpoint_saves_the_session_the_team_holds(self):
        saved: list[Any] = []

        async def _asave(session) -> None:
            saved.append(session)

        team = SimpleNamespace(cached_session="the-session", asave_session=_asave)

        await _Rig().controller.checkpoint(team)  # type: ignore[arg-type]

        assert saved == ["the-session"]

    async def test_a_team_with_no_session_yet_saves_nothing(self):
        saved: list[Any] = []

        async def _asave(session) -> None:
            saved.append(session)

        team = SimpleNamespace(cached_session=None, asave_session=_asave)

        await _Rig().controller.checkpoint(team)  # type: ignore[arg-type]

        assert saved == []

    async def test_the_periodic_checkpoint_saves_on_every_tick_until_cancelled(self):
        rig = _Rig()
        del rig.controller.__dict__["periodic_checkpoint"]
        ticks: list[Any] = []

        async def _checkpoint(team) -> None:
            ticks.append(team)

        rig.controller.checkpoint = _checkpoint  # type: ignore[method-assign]
        team = SimpleNamespace()

        task = asyncio.create_task(rig.controller.periodic_checkpoint(team, interval=0.01))  # type: ignore[arg-type]
        await _until(lambda: len(ticks) >= 2)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert ticks[:2] == [team, team]


class TestAResumedRun:
    async def test_a_resumed_stream_is_streamed_and_ends_the_phase_done(self):
        rig = _Rig()

        async def resumed():
            yield msg.ContentDelta(text="again")

        got = [m async for m in rig.controller.continue_run(resumed())]

        assert [m.text for m in got] == ["again"]
        assert rig.controller.phase is RunPhase.done
        assert rig.controller.current_run_task is None

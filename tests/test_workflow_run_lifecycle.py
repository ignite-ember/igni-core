"""One workflow run from first event to terminal record, and the runner around it.

:class:`WorkflowRun`, :class:`WorkflowRunRegistry` and
:class:`WorkflowRunner` are driven for real over a fake subprocess whose
stdout the test feeds and whose exit it decides; the session is a double
that records what is appended. What is asserted is what a window would
see: the records pushed, the record persisted, the phase, the reply the
script receives, and what is left live.
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from igni_core.backend import workflow_run, workflow_run_registry
from igni_core.backend.schemas_workflow_events import WorkflowEventRecord
from igni_core.backend.schemas_workflows import (
    WorkflowCancelOutcome,
    WorkflowMeta,
    WorkflowMetaEnvelope,
    WorkflowRunIdentity,
    WorkflowRunPhase,
    WorkflowRunRequest,
    WorkflowStartResult,
)
from igni_core.backend.workflow_discovery import WorkflowDiscovery
from igni_core.backend.workflow_run import WorkflowEventSink, WorkflowRun
from igni_core.backend.workflow_run_registry import CLOSE_WAIT_SECONDS, WorkflowRunRegistry
from igni_core.backend.workflow_runner import WorkflowRunner
from igni_core.backend.workflow_runtime_process import WorkflowRuntime, WorkflowRuntimeProcess
from igni_core.core.activity import ActivityRegistry, BackgroundTasks
from igni_core.core.config.group_policy.gate import RunGate
from igni_core.core.session.event_log_schema import SessionEventAppendResult


def _line(type_: str, payload: dict, seq: int = 0, run_id: str = "wf_1") -> bytes:
    event = {"ts": 1, "run_id": run_id, "seq": seq, "type": type_, "payload": payload}
    return json.dumps(event).encode() + b"\n"


def _request(req_id: str, prompt: str = "do it", seq: int = 1) -> bytes:
    return _line("agent_request", {"id": req_id, "prompt": prompt}, seq=seq)


class _Stdout:
    """Lines the test feeds; ``readline`` waits for the next one."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()

    def feed(self, *lines: bytes) -> None:
        for line in lines:
            self._queue.put_nowait(line)

    def close(self) -> None:
        self._queue.put_nowait(b"")

    async def readline(self) -> bytes:
        return await self._queue.get()


class _Stdin:
    def __init__(self, proc: _Process) -> None:
        self._proc = proc
        self.closing = False
        self.lines: list[dict] = []

    def write(self, data: bytes) -> None:
        message = json.loads(data)
        self.lines.append(message)
        if message["type"] == "cancel" and self._proc.exit_on_cancel:
            self._proc.exit(-15)

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return self.closing


class _Process:
    """A subprocess that runs until :meth:`exit`; ``exit_on_cancel`` obeys a cancel line."""

    def __init__(self, *, exit_on_cancel: bool = True) -> None:
        self.stdout = _Stdout()
        self.stdin = _Stdin(self)
        self.exit_on_cancel = exit_on_cancel
        self.returncode: int | None = None
        self.stopped = False
        self._exited = asyncio.Event()
        self._code = 0

    def exit(self, code: int = 0) -> None:
        self._code = code
        self._exited.set()

    async def wait(self) -> int:
        await self._exited.wait()
        self.returncode = self._code
        return self._code

    def terminate(self) -> None:
        self.exit(-15)

    def kill(self) -> None:
        self.exit(-9)


class _Session:
    """Records what the run appends; ``main_team`` answers agent calls."""

    def __init__(self, *, append=None) -> None:
        self.session_id = "s-1"
        self.appended: list[WorkflowEventRecord] = []
        self._append = append
        self.main_team = SimpleNamespace(arun=AsyncMock(return_value="done"))

    async def append_event(self, draft) -> SessionEventAppendResult:
        if self._append is not None:
            return await self._append(draft)
        self.appended.append(draft.payload)
        return SessionEventAppendResult(persisted=True)


def _run(
    proc: _Process,
    session: _Session,
    push: MagicMock | None = None,
    *,
    gate: RunGate | None = None,
    run_id: str = "wf_1",
) -> WorkflowRun:
    return WorkflowRun.for_session(
        identity=WorkflowRunIdentity(workflow_run_id=run_id, name="smoke"),
        process=WorkflowRuntimeProcess(proc),
        session=session,  # type: ignore[arg-type]
        push=push or MagicMock(),
        gate=gate or RunGate(),
    )


async def _until(condition, *, seconds: float = 3.0) -> None:
    for _ in range(int(seconds / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    pytest.fail("condition not reached")


# ── The sink: persist, then push ──


async def test_a_failed_persist_is_a_logged_warning_and_the_windows_still_get_the_record(
    caplog,
) -> None:
    async def failing(draft):
        return SessionEventAppendResult(persisted=False, error="disk full")

    push = MagicMock()
    sink = WorkflowEventSink(_Session(append=failing), push)  # type: ignore[arg-type]
    record = WorkflowEventRecord.synthetic(
        WorkflowRunIdentity(workflow_run_id="wf_7", name="n"), WorkflowRunPhase.COMPLETED, 0
    )

    with caplog.at_level(logging.WARNING, logger=workflow_run.__name__):
        await sink.publish(record)

    push.workflow_event.assert_called_once_with(record)
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning.getMessage() == "workflow event wf_7 not persisted: disk full"
    assert warning.exc_info is None


async def test_a_persisted_record_is_pushed_without_a_warning(caplog) -> None:
    session, push = _Session(), MagicMock()
    sink = WorkflowEventSink(session, push)  # type: ignore[arg-type]
    record = WorkflowEventRecord.synthetic(
        WorkflowRunIdentity(workflow_run_id="wf_7", name="n"), WorkflowRunPhase.FAILED, 3
    )

    with caplog.at_level(logging.WARNING, logger=workflow_run.__name__):
        await sink.publish(record)

    assert session.appended == [record]
    push.workflow_event.assert_called_once_with(record)
    assert caplog.records == []


async def test_an_append_that_raises_is_not_swallowed_and_nothing_is_pushed() -> None:
    async def raising(draft):
        raise RuntimeError("store gone")

    push = MagicMock()
    sink = WorkflowEventSink(_Session(append=raising), push)  # type: ignore[arg-type]
    record = WorkflowEventRecord.synthetic(
        WorkflowRunIdentity(workflow_run_id="wf_7", name="n"), WorkflowRunPhase.COMPLETED, 0
    )

    with pytest.raises(RuntimeError, match="store gone"):
        await sink.publish(record)

    push.workflow_event.assert_not_called()


# ── The run: drain, answer, settle ──


async def test_the_drain_publishes_events_in_order_and_settles_on_the_terminal_one() -> None:
    proc, session, push = _Process(), _Session(), MagicMock()
    run = _run(proc, session, push)
    assert run.phase is WorkflowRunPhase.RUNNING
    proc.stdout.feed(
        _line("workflow_started", {"name": "smoke"}, seq=0),
        _line("phase_started", {"phase_id": "p", "title": "T"}, seq=1),
        _line("workflow_failed", {"error": "boom"}, seq=2),
    )
    proc.stdout.close()

    await run.drain()

    assert [r.type for r in session.appended] == [
        "workflow_started",
        "phase_started",
        "workflow_failed",
    ]
    assert [r.seq for r in session.appended] == [0, 1, 2]
    assert [c.args[0].type for c in push.workflow_event.call_args_list] == [
        "workflow_started",
        "phase_started",
        "workflow_failed",
    ]
    assert run.phase is WorkflowRunPhase.FAILED


async def test_start_drain_runs_the_drain_as_a_named_task() -> None:
    proc, session = _Process(), _Session()
    run = _run(proc, session, run_id="wf_named")
    proc.stdout.feed(_line("workflow_started", {"name": "smoke"}, run_id="wf_named"))
    proc.stdout.close()

    run.start_drain()
    task = run._drain_task  # noqa: SLF001 — the task's name is the subject
    assert task is not None
    assert task.get_name() == "workflow-drain-wf_named"  # type: ignore[attr-defined]
    await task

    assert [r.type for r in session.appended] == ["workflow_started"]


async def test_an_agent_request_is_answered_from_the_team_and_never_published_as_a_record() -> None:
    proc, session = _Process(), _Session()
    run = _run(proc, session)
    proc.stdout.feed(_request("req_a", "summarise"))
    proc.stdout.close()

    await run.drain()
    await _until(lambda: proc.stdin.lines)

    session.main_team.arun.assert_awaited_once_with("summarise", stream=False)
    assert proc.stdin.lines == [
        {"type": "agent_response", "id": "req_a", "ok": True, "result": "done", "error": None}
    ]
    assert session.appended == []


async def test_a_closed_run_gate_refuses_the_agent_call_with_its_reason() -> None:
    gate = RunGate()
    gate.close("group policy required and no verified pack")
    proc, session = _Process(), _Session()
    run = _run(proc, session, gate=gate)
    proc.stdout.feed(_request("req_gated"))
    proc.stdout.close()

    await run.drain()
    await _until(lambda: proc.stdin.lines)

    [reply] = proc.stdin.lines
    assert reply["id"] == "req_gated"
    assert reply["ok"] is False
    assert reply["error"] == "group policy required and no verified pack"
    session.main_team.arun.assert_not_called()


async def test_a_slow_agent_answer_does_not_stop_the_drain_from_reading_and_answers_run_together() -> (
    None
):
    proc, session = _Process(), _Session()
    release = asyncio.Event()
    started: list[str] = []

    async def arun(prompt, stream):
        started.append(prompt)
        await release.wait()
        return f"answer:{prompt}"

    session.main_team.arun = arun
    run = _run(proc, session)
    proc.stdout.feed(
        _request("req_1", "first"),
        _request("req_2", "second", seq=2),
        _line("log", {"phase_id": None, "text": "read while waiting"}, seq=3),
    )
    proc.stdout.close()

    await run.drain()  # returns although neither answer is ready

    await _until(lambda: len(started) == 2)
    assert [r.type for r in session.appended] == ["log"]
    assert proc.stdin.lines == []
    release.set()
    await _until(lambda: len(proc.stdin.lines) == 2)
    assert {m["id"]: m["result"] for m in proc.stdin.lines} == {
        "req_1": "answer:first",
        "req_2": "answer:second",
    }


async def test_a_reply_for_a_closed_stdin_is_dropped_with_a_warning(caplog) -> None:
    proc, session = _Process(), _Session()
    proc.stdin.closing = True
    run = _run(proc, session, run_id="wf_closed")

    with caplog.at_level(logging.WARNING, logger=workflow_run.__name__):
        proc.stdout.feed(_request("req_x"))
        proc.stdout.close()
        await run.drain()
        await _until(lambda: any("dropped" in r.getMessage() for r in caplog.records))

    assert proc.stdin.lines == []
    [warning] = [r.getMessage() for r in caplog.records if "dropped" in r.getMessage()]
    assert warning == "agent response for wf_closed dropped: subprocess stdin closed"


async def test_a_drain_that_hits_an_unexpected_error_logs_it_and_finish_still_ends_the_run(
    caplog,
) -> None:
    async def failing(draft):
        raise RuntimeError("append broke")

    proc = _Process()
    failing_session = _Session(append=failing)
    run = _run(proc, failing_session, run_id="wf_err")
    proc.stdout.feed(_line("workflow_started", {"name": "smoke"}))
    run.start_drain()

    with caplog.at_level(logging.ERROR, logger=workflow_run.__name__):
        await _until(lambda: run._drain_task.done())  # type: ignore[union-attr]  # noqa: SLF001

    assert "workflow stdout drain failed for wf_err" in caplog.text
    assert run.phase is WorkflowRunPhase.RUNNING


async def test_cancelling_the_drain_task_propagates() -> None:
    proc = _Process()
    run = _run(proc, _Session())
    run.start_drain()
    task = run._drain_task  # noqa: SLF001
    assert task is not None
    await asyncio.sleep(0)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


# ── finish: the terminal record ──


async def test_a_process_that_exits_zero_having_said_nothing_ends_on_a_synthetic_completion() -> (
    None
):
    proc, session, push = _Process(), _Session(), MagicMock()
    run = _run(proc, session, push)
    proc.stdout.close()
    run.start_drain()
    proc.exit(0)

    await run.finish()

    [record] = session.appended
    assert record.type == "workflow_completed"
    assert record.payload == {"status": "completed", "exit_code": 0, "synthetic": True}
    assert record.seq == -1
    push.workflow_event.assert_called_once_with(record)
    assert run.phase is WorkflowRunPhase.COMPLETED


async def test_a_negative_exit_that_nobody_asked_for_is_a_failure() -> None:
    proc, session = _Process(), _Session()
    run = _run(proc, session)
    proc.stdout.close()
    run.start_drain()
    proc.exit(-9)

    await run.finish()

    [record] = session.appended
    assert record.type == "workflow_failed"
    assert record.payload["exit_code"] == -9
    assert run.phase is WorkflowRunPhase.FAILED


async def test_a_terminal_event_followed_by_a_non_zero_exit_adds_no_second_record() -> None:
    proc, session = _Process(), _Session()
    run = _run(proc, session)
    proc.stdout.feed(_line("workflow_completed", {"status": "completed"}))
    proc.stdout.close()
    run.start_drain()
    proc.exit(1)

    await run.finish()

    assert [r.type for r in session.appended] == ["workflow_completed"]
    assert run.phase is WorkflowRunPhase.COMPLETED


async def test_a_cancel_that_races_a_completion_keeps_the_completion() -> None:
    proc, session = _Process(), _Session()
    run = _run(proc, session)
    proc.stdout.feed(_line("workflow_completed", {"status": "completed", "result": 1}))
    run.start_drain()
    await _until(lambda: run.phase is WorkflowRunPhase.COMPLETED)

    assert await run.cancel() is WorkflowCancelOutcome.CANCELLED  # the process had not exited yet
    proc.stdout.close()
    await run.finish()

    assert run.phase is WorkflowRunPhase.COMPLETED
    assert [r.type for r in session.appended] == ["workflow_completed"]


async def test_cancelling_a_process_that_has_exited_says_so_and_changes_nothing() -> None:
    proc, session = _Process(), _Session()
    proc.returncode = 0
    run = _run(proc, session)

    assert await run.cancel() is WorkflowCancelOutcome.ALREADY_EXITED

    assert run.phase is WorkflowRunPhase.RUNNING
    assert proc.stdin.lines == []


async def test_a_cancel_is_a_cancel_line_then_a_cancelled_phase_and_a_cancelled_record() -> None:
    proc, session = _Process(), _Session()
    run = _run(proc, session)
    proc.stdout.close()
    run.start_drain()

    assert await run.cancel() is WorkflowCancelOutcome.CANCELLED
    await run.finish()

    assert proc.stdin.lines == [{"type": "cancel"}]
    [record] = session.appended
    assert record.type == "workflow_completed"
    assert record.payload["status"] == "cancelled"
    assert run.phase is WorkflowRunPhase.CANCELLED


async def test_finish_does_not_wait_forever_for_a_stalled_drain_and_ends_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workflow_run, "DRAIN_FLUSH_SECONDS", 0.05)
    proc, session = _Process(), _Session()
    run = _run(proc, session)
    run.start_drain()  # stdout never closes, so the drain never ends by itself
    proc.exit(0)

    began = asyncio.get_running_loop().time()
    await asyncio.wait_for(run.finish(), timeout=10)
    assert asyncio.get_running_loop().time() - began < 1.0

    [record] = session.appended
    assert record.payload["synthetic"] is True
    task = run._drain_task  # noqa: SLF001
    await asyncio.sleep(0)
    assert task is not None and task.done()


async def test_finish_without_a_started_drain_still_ends_on_a_terminal_record() -> None:
    proc, session = _Process(), _Session()
    run = _run(proc, session)
    proc.exit(5)

    await run.finish()

    [record] = session.appended
    assert record.type == "workflow_failed"
    assert record.payload["exit_code"] == 5


# ── The registry ──


def _registry() -> tuple[WorkflowRunRegistry, ActivityRegistry]:
    activity = ActivityRegistry()
    return WorkflowRunRegistry(tasks=BackgroundTasks(registry=activity)), activity


def test_the_close_wait_covers_a_stops_escalation_the_drain_flush_and_a_margin() -> None:
    assert CLOSE_WAIT_SECONDS == 14.0


async def test_a_run_whose_terminal_record_cannot_be_written_still_leaves_the_registry() -> None:
    async def failing(draft):
        raise RuntimeError("store gone")

    registry, activity = _registry()
    proc = _Process()
    proc.stdout.close()
    run = _run(proc, _Session(append=failing), run_id="wf_unwritable")
    registry.admit(run)

    proc.exit(0)
    await _until(lambda: "wf_unwritable" not in registry)

    assert len(registry) == 0
    assert activity.busy() == []


async def test_admit_that_cannot_start_its_supervisor_leaves_no_live_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, activity = _registry()
    proc = _Process()
    proc.stdout.close()
    run = _run(proc, _Session(), run_id="wf_nostart")

    def refuse(*args, **kwargs):
        args[0].close()
        raise RuntimeError("no loop")

    monkeypatch.setattr(registry._tasks, "start", refuse)  # noqa: SLF001

    with pytest.raises(RuntimeError, match="no loop"):
        registry.admit(run)

    assert "wf_nostart" not in registry
    assert len(registry) == 0
    assert activity.busy() == []


async def test_close_logs_each_run_that_will_not_end_and_returns(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    monkeypatch.setattr(workflow_run_registry, "CLOSE_WAIT_SECONDS", 0.1)
    stalled = asyncio.Event()

    async def stalls(draft):
        await stalled.wait()  # the terminal record's persist never returns
        return SessionEventAppendResult(persisted=True)

    registry, _ = _registry()
    proc = _Process()
    proc.stdout.close()
    registry.admit(_run(proc, _Session(append=stalls), run_id="wf_stuck"))

    with caplog.at_level(logging.WARNING, logger=workflow_run_registry.__name__):
        await asyncio.wait_for(registry.cancel_all(), timeout=3)

    assert "wf_stuck" in registry
    [warning] = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warning == "workflow workflow-await-wf_stuck did not end on close"
    stalled.set()
    await _until(lambda: "wf_stuck" not in registry)


async def test_a_failed_stop_is_logged_with_its_traceback_and_the_others_still_stop(
    caplog,
) -> None:
    registry, _ = _registry()
    broken, fine = _Process(), _Process()
    broken.stdin.drain = AsyncMock(side_effect=RuntimeError("stdin broke"))  # type: ignore[method-assign]
    for proc in (broken, fine):
        proc.stdout.close()
    registry.admit(_run(broken, _Session(), run_id="wf_broken"))
    registry.admit(_run(fine, _Session(), run_id="wf_fine"))

    with caplog.at_level(logging.ERROR, logger=workflow_run_registry.__name__):
        await registry.cancel_all()

    assert len(registry) == 0
    assert fine.stdin.lines == [{"type": "cancel"}]
    [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert record.getMessage() == "error cancelling workflow wf_broken"
    assert record.exc_info is not None and str(record.exc_info[1]) == "stdin broke"


# ── The runner ──


def _runner(
    tmp_path,
    *,
    resolved: WorkflowMetaEnvelope | None,
    runtime_present: bool = True,
    process: _Process | None = None,
    gate: RunGate | None = None,
) -> tuple[WorkflowRunner, MagicMock, ActivityRegistry]:
    discovery = WorkflowDiscovery.for_project(tmp_path, home_dir=tmp_path / "home")
    discovery.resolve = AsyncMock(return_value=resolved)  # type: ignore[method-assign]
    runtime_file = tmp_path / "runtime.mjs"
    if runtime_present:
        runtime_file.write_text("")
    runtime = WorkflowRuntime(runtime_path=runtime_file, cwd=tmp_path)
    runtime.start = AsyncMock(  # type: ignore[method-assign]
        return_value=WorkflowRuntimeProcess(process or _Process())
    )
    registry, activity = _registry()
    push = MagicMock()
    runner = WorkflowRunner(
        discovery=discovery, runtime=runtime, push=push, registry=registry, gate=gate or RunGate()
    )
    return runner, push, activity


_ENVELOPE = WorkflowMetaEnvelope(
    meta=WorkflowMeta(name="smoke", description="d"), path=".claude/workflows/smoke.mjs"
)


async def test_running_an_unknown_workflow_refuses_with_the_discovery_message_and_spawns_nothing(
    tmp_path,
) -> None:
    runner, _, _ = _runner(tmp_path, resolved=None)

    result = await runner.run(request=WorkflowRunRequest(name="ghost"), session=_Session())  # type: ignore[arg-type]

    assert result == WorkflowStartResult.not_found(runner._discovery.not_found_message("ghost"))  # noqa: SLF001
    assert result.failure == "not_found"
    runner._runtime.start.assert_not_called()  # type: ignore[attr-defined]  # noqa: SLF001
    assert not runner.has_live_runs()


async def test_running_without_the_bridge_refuses_as_runtime_missing_and_admits_nothing(
    tmp_path,
) -> None:
    runner, _, _ = _runner(tmp_path, resolved=_ENVELOPE, runtime_present=False)

    result = await runner.run(request=WorkflowRunRequest(name="smoke"), session=_Session())  # type: ignore[arg-type]

    assert result.failure == "runtime_missing"
    assert result.reason == runner._runtime.missing_message  # noqa: SLF001
    runner._runtime.start.assert_not_called()  # type: ignore[attr-defined]  # noqa: SLF001
    assert not runner.has_live_runs()


async def test_a_spawn_failure_reaches_the_caller_and_leaves_nothing_live(tmp_path) -> None:
    runner, _, activity = _runner(tmp_path, resolved=_ENVELOPE)
    runner._runtime.start = AsyncMock(side_effect=FileNotFoundError("node"))  # type: ignore[method-assign]  # noqa: SLF001

    with pytest.raises(FileNotFoundError):
        await runner.run(request=WorkflowRunRequest(name="smoke"), session=_Session())  # type: ignore[arg-type]

    assert not runner.has_live_runs()
    assert activity.busy() == []


async def test_a_started_run_is_live_with_a_fresh_id_for_the_sessions_own_id_and_args(
    tmp_path,
) -> None:
    proc = _Process()
    runner, _, activity = _runner(tmp_path, resolved=_ENVELOPE, process=proc)
    session = _Session()
    session.session_id = "s-real"  # a real Session has ``session_id`` and no ``id``

    result = await runner.run(
        request=WorkflowRunRequest(name="smoke", args={"k": [1, {"z": None}]}),
        session=session,  # type: ignore[arg-type]
    )

    assert result.ok
    assert result.name == "smoke"
    assert result.workflow_run_id.startswith("wf_")
    assert runner.is_live(result.workflow_run_id)
    assert not runner.is_live("wf_other")
    assert runner.has_live_runs()
    assert [lease.holder for lease in activity.busy()] == [f"workflow:{result.workflow_run_id}"]
    kwargs = runner._runtime.start.await_args.kwargs  # type: ignore[attr-defined]  # noqa: SLF001
    assert kwargs == {"session_id": "s-real", "args": {"k": [1, {"z": None}]}}
    proc.stdout.close()
    proc.exit(0)
    await _until(lambda: not runner.has_live_runs())


async def test_a_run_started_by_the_runner_is_refused_agent_calls_while_the_gate_is_closed(
    tmp_path,
) -> None:
    gate = RunGate()
    gate.close("closed by policy")
    proc = _Process()
    runner, _, _ = _runner(tmp_path, resolved=_ENVELOPE, process=proc, gate=gate)
    session = _Session()
    await runner.run(request=WorkflowRunRequest(name="smoke"), session=session)  # type: ignore[arg-type]

    proc.stdout.feed(_request("req_g"))
    await _until(lambda: proc.stdin.lines)

    assert proc.stdin.lines[0]["ok"] is False
    assert proc.stdin.lines[0]["error"] == "closed by policy"
    session.main_team.arun.assert_not_called()
    proc.stdout.close()
    proc.exit(0)
    await _until(lambda: not runner.has_live_runs())


async def test_cancelling_through_the_runner_gives_each_outcome(tmp_path) -> None:
    proc = _Process()
    runner, _, _ = _runner(tmp_path, resolved=_ENVELOPE, process=proc)
    started = await runner.run(request=WorkflowRunRequest(name="smoke"), session=_Session())  # type: ignore[arg-type]

    assert await runner.cancel("wf_unknown") is WorkflowCancelOutcome.NOT_LIVE
    proc.stdout.close()
    assert await runner.cancel(started.workflow_run_id) is WorkflowCancelOutcome.CANCELLED
    await _until(lambda: not runner.has_live_runs())


async def test_close_cancels_every_live_run_and_returns_when_they_have_ended(tmp_path) -> None:
    first, second = _Process(), _Process()
    runner, _, activity = _runner(tmp_path, resolved=_ENVELOPE, process=first)
    await runner.run(request=WorkflowRunRequest(name="smoke"), session=_Session())  # type: ignore[arg-type]
    runner._runtime.start = AsyncMock(return_value=WorkflowRuntimeProcess(second))  # type: ignore[method-assign]  # noqa: SLF001
    await runner.run(request=WorkflowRunRequest(name="smoke"), session=_Session())  # type: ignore[arg-type]
    for proc in (first, second):
        proc.stdout.close()
    assert runner.has_live_runs()

    await asyncio.wait_for(runner.close(), timeout=3)

    assert not runner.has_live_runs()
    assert activity.busy() == []
    assert first.stdin.lines == [{"type": "cancel"}]
    assert second.stdin.lines == [{"type": "cancel"}]


async def test_close_with_nothing_live_returns_at_once(tmp_path) -> None:
    runner, _, _ = _runner(tmp_path, resolved=None)

    await asyncio.wait_for(runner.close(), timeout=1)

    assert not runner.has_live_runs()

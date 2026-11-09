"""The live runs of one router: admitted, cancelled, closed.

:class:`WorkflowRunRegistry` keeps each run until its process exits and
its terminal record is written. These tests drive it with a real
:class:`BackgroundTasks` over its own :class:`ActivityRegistry`, and fake
only the subprocess.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from igni_core.backend.schemas_workflows import (
    WorkflowCancelOutcome,
    WorkflowRunIdentity,
    WorkflowRunPhase,
    WorkflowStartResult,
)
from igni_core.backend.workflow_discovery import WorkflowDiscovery
from igni_core.backend.workflow_run import WorkflowRun
from igni_core.backend.workflow_run_registry import WorkflowRunRegistry
from igni_core.backend.workflow_runtime_process import WorkflowRuntime, WorkflowRuntimeProcess
from igni_core.core.activity import ActivityRegistry, BackgroundTasks
from igni_core.core.session.event_log_schema import SessionEventAppendResult


class _Stdout:
    async def readline(self) -> bytes:
        return b""


class _Stdin:
    """Exits the process when the cancel line arrives; can fail after it."""

    def __init__(self, proc: _Process, *, fail: bool) -> None:
        self._proc = proc
        self._fail = fail

    def write(self, data: bytes) -> None:
        self._proc.exit(-15)

    async def drain(self) -> None:
        if self._fail:
            raise RuntimeError("stdin broke")

    def is_closing(self) -> bool:
        return False


class _Process:
    """A subprocess that runs until :meth:`exit` is called."""

    def __init__(self, *, fail_stop: bool = False) -> None:
        self.stdout = _Stdout()
        self.stdin = _Stdin(self, fail=fail_stop)
        self.returncode: int | None = None
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


def _session() -> MagicMock:
    session = MagicMock()
    session.append_event = AsyncMock(return_value=SessionEventAppendResult(persisted=True))
    return session


def _run(proc: _Process, run_id: str, session: MagicMock | None = None) -> WorkflowRun:
    return WorkflowRun.for_session(
        identity=WorkflowRunIdentity(workflow_run_id=run_id, name="smoke"),
        process=WorkflowRuntimeProcess(proc),
        session=session or _session(),  # type: ignore[arg-type]
        push=MagicMock(),
    )


def _registry() -> tuple[WorkflowRunRegistry, ActivityRegistry]:
    activity = ActivityRegistry()
    return WorkflowRunRegistry(tasks=BackgroundTasks(registry=activity)), activity


async def _settle(registry: WorkflowRunRegistry, run_id: str) -> None:
    for _ in range(100):
        if run_id not in registry:
            return
        await asyncio.sleep(0.01)


async def test_an_admitted_run_is_live_and_busy_until_it_finishes() -> None:
    registry, activity = _registry()
    proc = _Process()

    registry.admit(_run(proc, "wf_a"))

    assert "wf_a" in registry
    assert len(registry) == 1
    assert [lease.holder for lease in activity.busy()] == ["workflow:wf_a"]
    proc.exit(0)
    await _settle(registry, "wf_a")
    assert "wf_a" not in registry
    assert len(registry) == 0
    assert activity.busy() == []


async def test_cancelling_an_unknown_run_is_not_live() -> None:
    registry, _ = _registry()

    assert await registry.cancel("wf_nope") is WorkflowCancelOutcome.NOT_LIVE


async def test_cancelling_a_run_whose_process_exited_says_so() -> None:
    registry, _ = _registry()
    proc = _Process()
    proc.returncode = 0
    registry.admit(_run(proc, "wf_done"))

    assert await registry.cancel("wf_done") is WorkflowCancelOutcome.ALREADY_EXITED
    proc.exit(0)
    await _settle(registry, "wf_done")


async def test_cancelling_a_live_run_cancels_it() -> None:
    registry, _ = _registry()
    proc = _Process()
    run = _run(proc, "wf_live")
    registry.admit(run)

    assert await registry.cancel("wf_live") is WorkflowCancelOutcome.CANCELLED
    await _settle(registry, "wf_live")
    assert run.phase is WorkflowRunPhase.CANCELLED


async def test_cancel_all_ends_every_run_even_when_one_stop_fails() -> None:
    registry, activity = _registry()
    sessions = [_session(), _session(), _session()]
    procs = [_Process(), _Process(fail_stop=True), _Process()]
    runs = [
        _run(proc, f"wf_{i}", session)
        for i, (proc, session) in enumerate(zip(procs, sessions, strict=True))
    ]
    for run in runs:
        registry.admit(run)

    await registry.cancel_all()

    # It returns only once every run has left the table, on its record.
    assert len(registry) == 0
    assert activity.busy() == []
    assert all(run.phase is WorkflowRunPhase.CANCELLED for run in runs)
    for session in sessions:
        record = session.append_event.await_args.args[0].payload
        assert record.payload["status"] == "cancelled"


async def test_cancel_all_with_nothing_live_returns_at_once() -> None:
    registry, _ = _registry()

    await asyncio.wait_for(registry.cancel_all(), timeout=1)


# ── The refusals the runner used to build itself ──


def test_a_missing_runtime_refuses_with_the_same_reason(tmp_path) -> None:
    runtime = WorkflowRuntime(runtime_path=tmp_path / "absent.mjs", cwd=tmp_path)

    assert runtime.preflight() == WorkflowStartResult.runtime_missing(runtime.missing_message)
    assert runtime.missing_message == (
        f"workflow runtime missing at {tmp_path / 'absent.mjs'}; "
        "the .mjs file is part of the repo, this is a build issue"
    )


def test_a_present_runtime_has_no_refusal(tmp_path) -> None:
    path = tmp_path / "runtime.mjs"
    path.write_text("")

    assert WorkflowRuntime(runtime_path=path, cwd=tmp_path).preflight() is None


def test_a_missing_workflow_refuses_with_the_discovery_message(tmp_path) -> None:
    discovery = WorkflowDiscovery.for_project(tmp_path, home_dir=tmp_path / "home")

    refusal = discovery.refuse_missing("ghost")

    assert refusal == WorkflowStartResult.not_found(discovery.not_found_message("ghost"))
    assert refusal.failure == "not_found"
    assert not refusal.ok

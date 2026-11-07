"""Unit tests for the BE workflow runner.

Covers the three pieces of the runner's surface that don't need a
live neo4j / live LLM:

1. :class:`WorkflowDiscovery` — metadata read from
   ``.claude/workflows/*.mjs`` via the runtime's ``--discovery``
   short-circuit. Mocked subprocess emits canned ``workflow_meta``
   lines; the discovery aggregates + caches by mtime.

2. :meth:`WorkflowRun.drain` — line-by-line event parsing, with the
   persisted + push side effects exercised against an in-memory
   append + a fake push bridge.

3. :class:`WorkflowAgentBridge` — an ``agent_request`` is answered
   with ``session.main_team.arun``, and :class:`WorkflowRun` writes
   the matching ``agent_response`` back to the subprocess stdin.

The e2e path (real subprocess, real session, real push) lives in
``tests/test_workflow_e2e.py`` and is gated behind
``@pytest.mark.integration``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from igni_core.backend.schemas_workflow_events import (
    AgentRequestEvent,
    AgentRequestPayload,
    WorkflowEventRecord,
    WorkflowRuntimeEventReader,
)
from igni_core.backend.schemas_workflows import (
    WorkflowCancelOutcome,
    WorkflowRunIdentity,
    WorkflowRunPhase,
    WorkflowRunRequest,
)
from igni_core.backend.workflow_agent_bridge import WorkflowAgentBridge
from igni_core.backend.workflow_discovery import WorkflowDiscovery
from igni_core.backend.workflow_run import WorkflowEventSink, WorkflowRun
from igni_core.backend.workflow_runner import WorkflowRunner
from igni_core.backend.workflow_runtime_process import (
    CANCEL_GRACE_SECONDS,
    WorkflowRuntimeProcess,
)
from igni_core.core.paths import CONFIG_DIR


def _meta_line(name: str = "smoke", phases: int = 2) -> str:
    """One well-formed ``workflow_meta`` line as the runtime emits it."""
    return json.dumps(
        {
            "ts": 0,
            "run_id": "discovery",
            "seq": 0,
            "type": "workflow_meta",
            "payload": {
                "meta": {
                    "name": name,
                    "description": f"smoke test {name}",
                    "whenToUse": "tests",
                    "phases": [{"title": f"P{i}", "detail": ""} for i in range(phases)],
                },
                "path": f".claude/workflows/{name}.mjs",
            },
        }
    )


def _event(type_: str, payload: dict, seq: int = 0, run_id: str = "wf_abc") -> str:
    return json.dumps(
        {
            "ts": 1_700_000_000_000 + seq,
            "run_id": run_id,
            "seq": seq,
            "type": type_,
            "payload": payload,
        }
    )


class _FakeStream:
    """Minimal subprocess stdout/stdin double for drain tests."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)
        self.written: list[bytes] = []

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return False


def _run(proc: object, session: object, push: object, run_id: str = "wf_abc") -> WorkflowRun:
    """A :class:`WorkflowRun` over a fake process, with real collaborators."""
    return WorkflowRun(
        identity=WorkflowRunIdentity(workflow_run_id=run_id, name="smoke"),
        process=WorkflowRuntimeProcess(proc),
        sink=WorkflowEventSink(session, push),  # type: ignore[arg-type]
        bridge=WorkflowAgentBridge(session),  # type: ignore[arg-type]
    )


def _agent_request(payload: dict, run_id: str = "wf_call") -> AgentRequestEvent:
    return AgentRequestEvent(
        ts=1, run_id=run_id, seq=0, payload=AgentRequestPayload.model_validate(payload)
    )


class _FakeProcess:
    def __init__(self, lines: list[bytes], exit_code: int = 0) -> None:
        self.stdout = _FakeStream(lines)
        self.stdin = self.stdout  # share for test simplicity
        self.returncode: int | None = None
        self._exit_code = exit_code
        self._waiters: list[asyncio.Future[int]] = []

    async def wait(self) -> int:
        self.returncode = self._exit_code
        return self._exit_code


async def test_discovery_aggregates_and_caches(tmp_path: Path) -> None:
    """Discovery reads each workflow's meta; second call is cached."""
    workflow_dir = tmp_path / ".claude" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "alpha.mjs").write_text("// alpha body\n")
    (workflow_dir / "beta.mjs").write_text("// beta body\n")

    # Patch the runtime call to return canned meta lines.
    calls: list[tuple[str, ...]] = []

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        # args: (node, runtime, path, --discovery, --workflow-run-id, discovery)
        calls.append(args)
        path = Path(args[2])
        name = path.stem
        line = _meta_line(name=name, phases=3)
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(
            return_value=(
                (line + "\n").encode("utf-8"),
                b"",
            )
        )
        return proc

    # MonkeyPatch undoes the patch on exit, so it does not leak.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(asyncio, "create_subprocess_exec", fake_exec)
        disc = WorkflowDiscovery.for_project(project_dir=tmp_path)
        first = await disc.list_workflows()
        assert [env.meta.name for env in first] == ["alpha", "beta"], (
            f"got {[e.meta.name for e in first]}"
        )
        # Discovery calls the runtime once per workflow file.
        assert len(calls) == 2
        # Second call reuses cache (no extra subprocess calls).
        second = await disc.list_workflows()
        assert second == first
        assert len(calls) == 2  # no new calls


async def test_discovery_skips_when_no_dir(tmp_path: Path) -> None:
    """Empty project: no workflows dir → empty list, no subprocess."""
    disc = WorkflowDiscovery.for_project(project_dir=tmp_path)
    assert await disc.list_workflows() == []


async def test_resolve_unknown_returns_none(tmp_path: Path) -> None:
    disc = WorkflowDiscovery.for_project(project_dir=tmp_path)
    assert await disc.resolve("does-not-exist") is None


async def test_user_layer_shadows_team_layer(tmp_path: Path) -> None:
    """A workflow with the same name in ``.claude/workflows`` (the
    deprecated fallback) and ``.igni/workflows`` resolves to the
    ``.igni`` copy. ``list_workflows`` returns one entry
    per name (no duplicates). The path field reports the
    user-layer location.
    """
    team_dir = tmp_path / ".claude" / "workflows"
    user_dir = tmp_path / CONFIG_DIR / "workflows"
    team_dir.mkdir(parents=True)
    user_dir.mkdir(parents=True)
    (team_dir / "shared.mjs").write_text("export const meta = { name: 'team-version' }\n")
    (user_dir / "shared.mjs").write_text("export const meta = { name: 'user-version' }\n")
    (user_dir / "personal.mjs").write_text("export const meta = { name: 'personal' }\n")

    async def fake_exec(*args, **kwargs):
        path = Path(args[2])
        # Read the file's meta line to determine which copy ran.
        with open(path) as f:
            first_line = f.readline().strip()
        if "team-version" in first_line:
            label = "team-version"
        else:
            label = "user-version" if "user-version" in first_line else "personal"
        line = json.dumps(
            {
                "ts": 0,
                "run_id": "discovery",
                "seq": 0,
                "type": "workflow_meta",
                "payload": {
                    "meta": {"name": label, "phases": []},
                    "path": str(path.relative_to(tmp_path)),
                },
            }
        )
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=((line + "\n").encode("utf-8"), b""))
        proc.returncode = 0
        return proc

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(asyncio, "create_subprocess_exec", fake_exec)
        disc = WorkflowDiscovery.for_project(project_dir=tmp_path)
        result = await disc.list_workflows()
        # Two unique workflows (one deduplicated by shadow).
        assert [env.meta.name for env in result] == ["personal", "user-version"]
        # The shadowed "shared" name resolves to user-version.
        shared = await disc.resolve("shared")
        assert shared is not None
        assert shared.meta.name == "user-version"
        assert shared.path == f"{CONFIG_DIR}/workflows/shared.mjs"


async def test_discovery_scans_both_layers_independently(tmp_path: Path) -> None:
    """The two layers are scanned independently — a project with
    only a user layer (no team layer) still works, and vice versa.
    """
    user_dir = tmp_path / CONFIG_DIR / "workflows"
    user_dir.mkdir(parents=True)
    (user_dir / "user-only.mjs").write_text("export const meta = { name: 'user-only' }\n")

    async def fake_exec(*args, **kwargs):
        path = Path(args[2])
        line = json.dumps(
            {
                "ts": 0,
                "run_id": "discovery",
                "seq": 0,
                "type": "workflow_meta",
                "payload": {
                    "meta": {"name": "user-only", "phases": []},
                    "path": str(path.relative_to(tmp_path)),
                },
            }
        )
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=((line + "\n").encode("utf-8"), b""))
        proc.returncode = 0
        return proc

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(asyncio, "create_subprocess_exec", fake_exec)
        disc = WorkflowDiscovery.for_project(project_dir=tmp_path)
        result = await disc.list_workflows()
        assert [env.meta.name for env in result] == ["user-only"]
        assert result[0].path == f"{CONFIG_DIR}/workflows/user-only.mjs"


async def test_event_drain_persists_and_pushes(tmp_path: Path) -> None:
    """Three stdout lines → three session.append_event + three push calls."""
    lines = [
        _event("workflow_started", {"name": "smoke"}, seq=0).encode("utf-8") + b"\n",
        _event("phase_started", {"phase_id": "p1", "title": "A"}, seq=1).encode("utf-8") + b"\n",
        _event("workflow_completed", {"status": "completed", "result": {"ok": True}}, seq=2).encode(
            "utf-8"
        )
        + b"\n",
    ]
    proc = _FakeProcess(lines, exit_code=0)

    push = MagicMock()
    session = MagicMock()
    session.append_event = AsyncMock()

    run = _run(proc, session, push)

    await run.drain()

    assert session.append_event.await_count == 3
    assert push.workflow_event.call_count == 3
    # Persisted envelope has the workflow_run_id + event type.
    first_payload = session.append_event.await_args_list[0].args[1]
    assert first_payload["workflow_run_id"] == "wf_abc"
    assert first_payload["type"] == "workflow_started"
    # The terminal event moved the phase, so finish() adds nothing.
    assert run.phase is WorkflowRunPhase.COMPLETED


async def test_synthetic_workflow_failed_on_non_zero_exit(tmp_path: Path) -> None:
    """Subprocess crash mid-run → synthetic workflow_failed event."""
    # Only the workflow_started line — the subprocess then dies.
    lines = [_event("workflow_started", {"name": "x"}, seq=0).encode("utf-8") + b"\n"]
    proc = _FakeProcess(lines, exit_code=2)

    push = MagicMock()
    session = MagicMock()
    session.append_event = AsyncMock()

    run = _run(proc, session, push, run_id="wf_boom")

    # Drain the started event first.
    await run.drain()
    # Then finish() notices the non-zero exit and synthesises the failure.
    await run.finish()

    # The synthetic event is the 2nd call (after the started line).
    types = [c.args[0] for c in session.append_event.await_args_list]
    payloads = [c.args[1] for c in session.append_event.await_args_list]
    assert types == ["workflow_event", "workflow_event"]
    assert payloads[1]["type"] == "workflow_failed"
    assert payloads[1]["payload"]["exit_code"] == 2
    # Pushed too.
    pushed: list[WorkflowEventRecord] = [c.args[0] for c in push.workflow_event.call_args_list]
    assert [r.type for r in pushed] == ["workflow_started", "workflow_failed"]
    assert pushed[1].payload["exit_code"] == 2
    assert run.phase is WorkflowRunPhase.FAILED


async def test_a_cancelled_run_without_a_terminal_event_ends_cancelled(tmp_path: Path) -> None:
    """A run stopped through cancel that exits without saying so ends on a
    synthetic ``workflow_completed{status: cancelled}``, not a failure."""
    proc = _FakeProcess([], exit_code=-15)
    proc.returncode = None
    push = MagicMock()
    session = MagicMock()
    session.append_event = AsyncMock()
    run = _run(proc, session, push, run_id="wf_stop")

    assert await run.cancel() is WorkflowCancelOutcome.CANCELLED
    await run.finish()

    record: WorkflowEventRecord = push.workflow_event.call_args.args[0]
    assert record.type == "workflow_completed"
    assert record.payload == {"status": "cancelled", "exit_code": -15, "synthetic": True}
    assert run.phase is WorkflowRunPhase.CANCELLED


async def test_agent_request_bridges_to_team(tmp_path: Path) -> None:
    """An agent_request is answered with team.arun, and the run writes
    the agent_response back to subprocess stdin."""
    proc = _FakeProcess([], exit_code=0)

    push = MagicMock()
    session = MagicMock()
    session.append_event = AsyncMock()
    team = MagicMock()
    team.arun = AsyncMock(return_value="agent result text")
    session.main_team = team

    raw = json.dumps(
        {
            "ts": 1,
            "run_id": "wf_call",
            "seq": 0,
            "type": "agent_request",
            "payload": {
                "id": "req_42",
                "prompt": "do the thing",
                "label": "test-agent",
                "phase": None,
                "timeout_seconds": 30,
            },
        }
    )
    event = WorkflowRuntimeEventReader.parse_line(raw)
    assert isinstance(event, AgentRequestEvent)

    reply = await WorkflowAgentBridge(session).answer(event)

    team.arun.assert_awaited_once()
    assert team.arun.await_args.args[0] == "do the thing"
    assert reply.ok is True
    assert reply.id == "req_42"
    assert reply.result == "agent result text"

    # Through the run, the reply is written to stdin as one JSON line.
    run = _run(proc, session, push, run_id="wf_call")
    await run._answer(event)
    written = json.loads(b"".join(proc.stdin.written).decode("utf-8"))  # type: ignore[attr-defined]
    assert written == {
        "type": "agent_response",
        "id": "req_42",
        "ok": True,
        "result": "agent result text",
        "error": None,
    }


async def test_agent_timeout_of_zero_means_no_timeout() -> None:
    """``workflow_runtime.mjs`` reads ``timeoutSeconds <= 0`` as no limit;
    the bridge passed 0 to ``wait_for``, which timed the call out at once."""
    session = MagicMock()
    session.main_team.arun = AsyncMock(return_value="done")
    seen: list[object] = []
    real_wait_for = asyncio.wait_for

    async def spy_wait_for(coro, timeout):  # type: ignore[no-untyped-def]
        seen.append(timeout)
        return await real_wait_for(coro, timeout=timeout)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(asyncio, "wait_for", spy_wait_for)
        reply = await WorkflowAgentBridge(session).answer(
            _agent_request({"id": "req_0", "prompt": "p", "timeout_seconds": 0})
        )

    assert seen == [None]
    assert reply.ok is True
    assert reply.result == "done"


async def test_a_dict_prompt_and_a_null_label_reach_team_arun() -> None:
    """``team.arun`` takes message dicts as well as strings, and JS passes a
    ``label: null`` through; neither is a reason to refuse the call."""
    session = MagicMock()
    session.main_team.arun = AsyncMock(return_value="done")
    prompt = {"role": "user", "content": "hi"}

    reply = await WorkflowAgentBridge(session).answer(
        _agent_request({"id": "req_dict", "prompt": prompt, "label": None})
    )

    assert reply.ok is True, reply.error
    session.main_team.arun.assert_awaited_once()
    assert session.main_team.arun.await_args.args[0] == prompt


async def test_a_malformed_agent_request_is_refused_not_left_waiting() -> None:
    session = MagicMock()
    session.main_team.arun = AsyncMock(return_value="never")

    reply = await WorkflowAgentBridge(session).answer(
        _agent_request({"id": "req_bad", "timeout_seconds": "soon"})
    )

    assert reply.ok is False
    assert reply.id == "req_bad"
    assert "agent request invalid" in (reply.error or "")
    session.main_team.arun.assert_not_awaited()


async def test_an_unserialisable_agent_result_is_refused() -> None:
    session = MagicMock()
    session.main_team.arun = AsyncMock(return_value=MagicMock(content=object()))

    reply = await WorkflowAgentBridge(session).answer(_agent_request({"id": "req_obj"}))

    assert reply.ok is False
    assert "not JSON-serialisable" in (reply.error or "")


@pytest.mark.parametrize(
    "line",
    [
        {"type": "workflow_completed", "payload": {"status": "cancelled", "duration_ms": 12}},
        {
            "type": "workflow_completed",
            "payload": {"status": "completed", "duration_ms": 40, "result": {"ok": True}},
        },
        {"type": "workflow_completed", "payload": {"status": "completed", "result": None}},
        {
            "type": "workflow_failed",
            "payload": {"error": "boom", "stack": "Error: boom\n  at x", "duration_ms": 3},
        },
        {"type": "workflow_failed", "payload": {"error": "boom", "duration_ms": 3, "extra": 1}},
        {"type": "phase_started", "payload": {"phase_id": "p1", "title": "A"}},
    ],
)
def test_forwarded_payloads_are_byte_identical(line: dict) -> None:
    """Each terminal shape ``workflow_runtime.mjs`` emits reaches the record
    exactly as sent: no defaults added, no unknown keys dropped."""
    raw = json.dumps({"ts": 5, "run_id": "wf_x", "seq": 3, **line})
    event = WorkflowRuntimeEventReader.parse_line(raw)
    assert event is not None

    record = WorkflowEventRecord.from_event(
        WorkflowRunIdentity(workflow_run_id="wf_x", name="smoke"), event
    )

    assert json.dumps(record.payload) == json.dumps(line["payload"])
    assert record.type == line["type"]


# ── Runner edge cases (cancel, cwd-relative, agent timeout) ──


async def test_runner_spawns_user_layer_workflow_with_cwd_relative_path(
    tmp_path: Path,
) -> None:
    """A workflow in ``.igni/workflows/`` is spawned with the
    same ``cwd=project_dir`` as a team-layer workflow, and the
    ``path`` field on the discovery envelope is a project-relative
    path that the runner resolves to an absolute path before
    passing it to the Node subprocess.

    This is the regression test for the user-layer feature:
    the user-layer path passes through the same spawn pipeline
    as the team-layer path.
    """
    user_dir = tmp_path / CONFIG_DIR / "workflows"
    user_dir.mkdir(parents=True)
    (user_dir / "personal.mjs").write_text("export const meta = { name: 'personal' }\n")

    captured: dict[str, object] = {}

    async def fake_exec(*args, **kwargs):
        if "--discovery" in args:
            path = Path(args[2])
            line = json.dumps(
                {
                    "ts": 0,
                    "run_id": "discovery",
                    "seq": 0,
                    "type": "workflow_meta",
                    "payload": {
                        "meta": {"name": path.stem, "phases": []},
                        "path": str(path.relative_to(tmp_path)),
                    },
                }
            )
            proc = _FakeProcess([], exit_code=0)
            proc.communicate = AsyncMock(return_value=((line + "\n").encode("utf-8"), b""))
            proc.returncode = 0
            return proc
        captured["args"] = args
        captured["cwd"] = kwargs.get("cwd")
        return _FakeProcess([], exit_code=0)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(asyncio, "create_subprocess_exec", fake_exec)
        runner = WorkflowRunner.for_project(project_dir=tmp_path, push=MagicMock())
        session = MagicMock()
        session.id = "sess_x"
        result = await runner.run(request=WorkflowRunRequest(name="personal"), session=session)
        assert result.ok
        run_id = result.workflow_run_id
        assert run_id.startswith("wf_")
        # The Node subprocess was spawned with the project dir as cwd.
        assert captured["cwd"] == str(tmp_path)
        # The path passed to the subprocess is the absolute path to
        # the workflow file (under the project root), not just the
        # ``.igni/workflows/personal.mjs`` relative string.
        path_arg = [a for a in captured["args"] if "personal" in str(a)][0]
        assert Path(path_arg).is_absolute()
        assert str(path_arg).endswith(f"{CONFIG_DIR}/workflows/personal.mjs")


async def test_runner_cancel_sends_cancel_message_and_sends_sigterm(tmp_path: Path) -> None:
    """``cancel()`` writes a ``{"type":"cancel"}`` line to the
    subprocess stdin, then waits for natural exit. If the subprocess
    doesn't exit within ``CANCEL_GRACE_SECONDS``, the process is sent
    SIGTERM, then SIGKILL as a last resort.

    We exercise the cancel-message path here; the SIGTERM/SIGKILL
    escalation is covered separately by the subprocess integration
    tests (which need a real Node — outside the unit-test surface).
    """
    proc = _FakeProcess([], exit_code=0)
    proc.returncode = None  # not yet exited

    written: list[bytes] = []

    class _CancelStream:
        def write(self, data: bytes) -> None:
            written.append(data)

        async def drain(self) -> None:
            return None

        def is_closing(self) -> bool:
            return False

    proc.stdin = _CancelStream()  # type: ignore[assignment]

    async def fake_wait(*a, **kw):
        # Subprocess exits cleanly after the cancel message.
        proc.returncode = 0
        return 0

    proc.wait = fake_wait  # type: ignore[method-assign]

    run = _run(proc, MagicMock(), MagicMock(), run_id="wf_cancel")
    real_wait_for = asyncio.wait_for

    async def fake_wait_for(coro, timeout):
        # Make sure the cancel message is written before we "wait"
        # for the subprocess to exit.
        if timeout == CANCEL_GRACE_SECONDS:
            return await coro
        return await real_wait_for(coro, timeout=0.1)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(asyncio, "wait_for", fake_wait_for)
        assert await run.cancel() is WorkflowCancelOutcome.CANCELLED

    # The cancel message was written to stdin.
    assert len(written) == 1
    assert json.loads(written[0].decode()) == {"type": "cancel"}
    assert run.phase is WorkflowRunPhase.CANCELLING


async def test_agent_bridge_timeout_fires_agent_response_error(
    tmp_path: Path,
) -> None:
    """If the agent call exceeds ``timeoutSeconds``, the bridge
    sends an ``agent_response{ok: false, error: timeout message}``
    and rejects the agent's promise — the workflow continues
    on the next event.
    """
    proc = _FakeProcess([], exit_code=0)
    session = MagicMock()

    # team.arun hangs forever; the bridge must time it out.
    async def hang(*a, **kw):
        await asyncio.sleep(60)
        return "should not return"

    session.main_team.arun = hang

    ev = _agent_request(
        {
            "id": "req_slow",
            "prompt": "slow",
            "label": "slow",
            "phase": "S",
            "timeout_seconds": 1,
        },
        run_id="wf_t",
    )

    written: list[bytes] = []

    class _Stream:
        def write(self, data: bytes) -> None:
            written.append(data)

        async def drain(self) -> None:
            return None

        def is_closing(self) -> bool:
            return False

    proc.stdin = _Stream()  # type: ignore[assignment]
    run = _run(proc, session, MagicMock(), run_id="wf_t")

    real_wait_for = asyncio.wait_for

    async def fast_wait_for(coro, timeout):
        # Don't actually wait the full timeout — force the timeout
        # path. We do this by setting the timeout to a tiny value
        # and letting ``wait_for`` raise TimeoutError naturally.
        if timeout > 1:
            return await real_wait_for(coro, timeout=0.05)
        return await real_wait_for(coro, timeout=timeout)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(asyncio, "wait_for", fast_wait_for)
        await run._answer(ev)

    # The bridge wrote an agent_response with ok=False and an error.
    assert len(written) == 1
    payload = json.loads(written[0].decode())
    assert payload["type"] == "agent_response"
    assert payload["id"] == "req_slow"
    assert payload["ok"] is False
    assert "timed out" in payload["error"]


async def test_runner_passes_args_to_subprocess_as_json_string(
    tmp_path: Path,
) -> None:
    """The runner serializes the ``args`` dict to JSON and passes
    it as the last CLI arg (``--args``). The Node runtime parses
    it back. This is the wire contract for the user-layer
    workflow's args object.
    """
    user_dir = tmp_path / CONFIG_DIR / "workflows"
    user_dir.mkdir(parents=True)
    (user_dir / "personal.mjs").write_text("export const meta = { name: 'personal' }\n")

    captured_args: list[str] = []

    async def fake_exec(*args, **kwargs):
        # Discovery subprocesses pass ``--discovery``; the run
        # subprocess passes ``--args <json>``. Handle both.
        if "--discovery" in args:
            path = Path(args[2])
            line = json.dumps(
                {
                    "ts": 0,
                    "run_id": "discovery",
                    "seq": 0,
                    "type": "workflow_meta",
                    "payload": {
                        "meta": {"name": path.stem, "phases": []},
                        "path": str(path.relative_to(tmp_path)),
                    },
                }
            )
            proc = _FakeProcess([], exit_code=0)
            proc.communicate = AsyncMock(return_value=((line + "\n").encode("utf-8"), b""))
            proc.returncode = 0
            return proc
        # Run subprocess: capture the --args value.
        i = list(args).index("--args")
        captured_args.append(args[i + 1])
        return _FakeProcess([], exit_code=0)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(asyncio, "create_subprocess_exec", fake_exec)
        runner = WorkflowRunner.for_project(project_dir=tmp_path, push=MagicMock())
        session = MagicMock()
        session.id = "sess_x"
        await runner.run(
            request=WorkflowRunRequest(
                name="personal", args={"file": "src/example.py", "tag": "demo"}
            ),
            session=session,
        )

    assert len(captured_args) == 1
    parsed = json.loads(captured_args[0])
    assert parsed == {"file": "src/example.py", "tag": "demo"}

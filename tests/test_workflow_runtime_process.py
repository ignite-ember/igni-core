"""The Node bridge's process: what it reads, what it writes, how it stops, how it probes.

:class:`WorkflowRuntimeProcess` takes anything shaped like an asyncio
subprocess, so these tests give it small fakes; :class:`WorkflowRuntime`
is driven with ``asyncio.create_subprocess_exec`` patched, and the argv
it assembles is read back.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from igni_core.backend import workflow_runtime_process as module
from igni_core.backend.schemas_workflow_events import (
    WorkflowCancelCommand,
    WorkflowCompletedEvent,
)
from igni_core.backend.schemas_workflows import (
    WorkflowMeta,
    WorkflowMetaEnvelope,
    WorkflowRunIdentity,
)
from igni_core.backend.workflow_runtime_process import (
    CANCEL_GRACE_SECONDS,
    RUNTIME_PATH,
    TERMINATE_GRACE_SECONDS,
    WorkflowRuntime,
    WorkflowRuntimeProcess,
)


def _line(type_: str, payload: dict, seq: int = 0) -> bytes:
    event = {"ts": 1, "run_id": "wf_x", "seq": seq, "type": type_, "payload": payload}
    return json.dumps(event).encode("utf-8") + b"\n"


class _Stdout:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


class _Stdin:
    def __init__(self, *, closing: bool = False, error: Exception | None = None) -> None:
        self.written: list[bytes] = []
        self._closing = closing
        self._error = error

    def write(self, data: bytes) -> None:
        if self._error is not None:
            raise self._error
        self.written.append(data)

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return self._closing


class _Stubborn:
    """A process that exits only on the signal named in ``obeys``."""

    def __init__(self, obeys: str) -> None:
        self.stdin = _Stdin()
        self.stdout = None
        self.returncode: int | None = None
        self.signals: list[str] = []
        self._obeys = obeys
        self._exited = asyncio.Event()
        if obeys == "cancel":
            self.stdin.write = self._on_cancel  # type: ignore[method-assign]

    def _on_cancel(self, data: bytes) -> None:
        self.signals.append("cancel")
        self._exit(0)

    def _exit(self, code: int) -> None:
        self.returncode = code
        self._exited.set()

    async def wait(self) -> int:
        await self._exited.wait()
        return self.returncode or 0

    def terminate(self) -> None:
        self.signals.append("terminate")
        if self._obeys == "terminate":
            self._exit(-15)

    def kill(self) -> None:
        self.signals.append("kill")
        self._exit(-9)


# ── Constants ──


def test_the_bridge_ships_next_to_the_module_and_the_graces_are_five_and_two() -> None:
    assert Path(module.__file__).parent / "workflow_runtime.mjs" == RUNTIME_PATH
    assert RUNTIME_PATH.is_file()
    assert CANCEL_GRACE_SECONDS == 5.0
    assert TERMINATE_GRACE_SECONDS == 2.0


# ── events() ──


async def test_events_skips_blank_noise_and_non_event_json_and_survives_bad_utf8(caplog) -> None:
    stdout = _Stdout(
        [
            b"\n",
            b"   \n",
            b"node: warning: something on merged stderr\n",
            b'{"type": "workflow_started"}\n',  # JSON, but not a valid event
            b"\xff\xfe broken bytes\n",
            _line("workflow_completed", {"status": "completed"}, seq=7),
        ]
    )
    process = WorkflowRuntimeProcess(SimpleNamespace(stdout=stdout))

    with caplog.at_level(logging.DEBUG, logger=module.__name__):
        events = [event async for event in process.events()]

    assert len(events) == 1
    assert isinstance(events[0], WorkflowCompletedEvent)
    assert events[0].seq == 7
    skipped = [r.getMessage() for r in caplog.records if "validation failed" in r.getMessage()]
    # Noise is logged, blank lines are not.
    assert len(skipped) == 3
    assert any("merged stderr" in message for message in skipped)


async def test_events_yields_nothing_when_there_is_no_stdout() -> None:
    process = WorkflowRuntimeProcess(SimpleNamespace(stdout=None))

    assert [event async for event in process.events()] == []


async def test_events_logs_only_the_first_two_hundred_characters_of_noise(caplog) -> None:
    long_noise = b"x" * 500 + b"\n"
    process = WorkflowRuntimeProcess(SimpleNamespace(stdout=_Stdout([long_noise])))

    with caplog.at_level(logging.DEBUG, logger=module.__name__):
        assert [event async for event in process.events()] == []

    [message] = [r.getMessage() for r in caplog.records if "validation failed" in r.getMessage()]
    assert message.endswith(": " + "x" * 200)


# ── send() ──


async def test_send_writes_one_json_line_and_says_so() -> None:
    stdin = _Stdin()
    process = WorkflowRuntimeProcess(SimpleNamespace(stdin=stdin))

    assert await process.send(WorkflowCancelCommand()) is True

    assert stdin.written == [b'{"type":"cancel"}\n']


@pytest.mark.parametrize(
    "stdin",
    [None, _Stdin(closing=True), _Stdin(error=BrokenPipeError("gone"))],
    ids=["no-stdin", "closing", "broken-pipe"],
)
async def test_send_says_false_when_stdin_is_gone(stdin) -> None:
    process = WorkflowRuntimeProcess(SimpleNamespace(stdin=stdin))

    assert await process.send(WorkflowCancelCommand()) is False
    if stdin is not None:
        assert stdin.written == []


# ── stop() ──


@pytest.fixture
def short_graces(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "CANCEL_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(module, "TERMINATE_GRACE_SECONDS", 0.05)


async def test_stop_asks_politely_first_and_signals_nothing_when_the_process_obeys(
    short_graces,
) -> None:
    proc = _Stubborn(obeys="cancel")

    await WorkflowRuntimeProcess(proc).stop()

    assert proc.signals == ["cancel"]


async def test_stop_terminates_a_process_that_ignores_the_cancel_line(short_graces) -> None:
    proc = _Stubborn(obeys="terminate")

    await WorkflowRuntimeProcess(proc).stop()

    assert proc.signals == ["terminate"]
    assert proc.returncode == -15


async def test_stop_kills_a_process_that_ignores_cancel_and_terminate(short_graces) -> None:
    proc = _Stubborn(obeys="kill")

    await asyncio.wait_for(WorkflowRuntimeProcess(proc).stop(), timeout=2)

    assert proc.signals == ["terminate", "kill"]
    assert proc.returncode == -9


async def test_stop_still_escalates_when_stdin_is_already_gone(short_graces) -> None:
    proc = _Stubborn(obeys="terminate")
    proc.stdin = None

    await WorkflowRuntimeProcess(proc).stop()

    assert proc.signals == ["terminate"]


async def test_wait_and_returncode_pass_through_to_the_process() -> None:
    proc = SimpleNamespace(returncode=3, wait=AsyncMock(return_value=3))
    process = WorkflowRuntimeProcess(proc)

    assert process.returncode == 3
    assert await process.wait() == 3


# ── WorkflowRuntime ──


def _meta_line(**meta: object) -> bytes:
    body = {"name": "smoke", "description": "d", "phases": []} | meta
    return _line("workflow_meta", {"meta": body, "path": ".claude/workflows/smoke.mjs"})


def _probe_exec(monkeypatch, *, stdout: bytes, stderr: bytes = b"", code: int = 0) -> list:
    calls: list = []

    async def fake_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(
            returncode=code, communicate=AsyncMock(return_value=(stdout, stderr))
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return calls


def _runtime(tmp_path: Path) -> WorkflowRuntime:
    path = tmp_path / "runtime.mjs"
    path.write_text("")
    return WorkflowRuntime(runtime_path=path, cwd=tmp_path)


async def test_probe_reads_the_meta_with_discovery_from_the_project_dir(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path)
    calls = _probe_exec(monkeypatch, stdout=b"noise\n" + _meta_line())

    result = await runtime.probe_meta(tmp_path / "wf.mjs")

    assert result.ok
    assert result.meta is not None and result.meta.name == "smoke"
    [(args, kwargs)] = calls
    assert args == (
        "node",
        str(tmp_path / "runtime.mjs"),
        str(tmp_path / "wf.mjs"),
        "--discovery",
        "--workflow-run-id",
        "discovery",
    )
    assert kwargs["cwd"] == str(tmp_path)


async def test_probe_gives_exit_code_and_the_first_400_characters_of_stderr(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path)
    _probe_exec(monkeypatch, stdout=b"", stderr=b"E" * 500, code=4)

    result = await runtime.probe_meta(tmp_path / "wf.mjs")

    assert not result.ok
    assert result.reason == "exit 4: " + "E" * 400


async def test_probe_says_when_the_meta_event_is_invalid(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    bad = _line("workflow_meta", {"meta": "not an object", "path": "p"})
    _probe_exec(monkeypatch, stdout=bad)

    result = await runtime.probe_meta(tmp_path / "wf.mjs")

    assert result.meta is None
    assert result.reason.startswith("meta invalid: ")


async def test_probe_skips_other_invalid_lines_and_finds_the_meta_after_them(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path)
    stdout = b"\n" + b"garbage\n" + b'{"type":"workflow_started"}\n' + _meta_line(name="later")
    _probe_exec(monkeypatch, stdout=stdout)

    result = await runtime.probe_meta(tmp_path / "wf.mjs")

    assert result.meta is not None and result.meta.name == "later"


async def test_probe_without_a_meta_line_says_so(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    _probe_exec(monkeypatch, stdout=b"just noise\n\n")

    assert (await runtime.probe_meta(tmp_path / "wf.mjs")).reason == "no workflow_meta line"


async def test_probe_with_the_runtime_missing_gives_its_reason_and_spawns_nothing(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    runtime = WorkflowRuntime(runtime_path=tmp_path / "absent.mjs", cwd=tmp_path)
    calls = _probe_exec(monkeypatch, stdout=b"")

    with caplog.at_level(logging.WARNING, logger=module.__name__):
        result = await runtime.probe_meta(tmp_path / "wf.mjs")

    assert result.reason == f"workflow runtime missing at {tmp_path / 'absent.mjs'}"
    assert calls == []
    assert any("workflow runtime missing" in r.getMessage() for r in caplog.records)


async def test_start_spawns_node_with_every_flag_stdin_piped_and_stderr_merged(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path)
    captured: dict = {}
    proc = SimpleNamespace()

    async def fake_exec(*args, **kwargs):
        captured["args"], captured["kwargs"] = args, kwargs
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    envelope = WorkflowMetaEnvelope(
        meta=WorkflowMeta(name="smoke", description="d"), path=".claude/workflows/smoke.mjs"
    )

    started = await runtime.start(
        WorkflowRunIdentity(workflow_run_id="wf_42", name="smoke"),
        envelope,
        session_id="sess-7",
        args={"naïve": "ünï", "deep": {"list": [1, None, True]}},
    )

    assert isinstance(started, WorkflowRuntimeProcess)
    args = captured["args"]
    assert args[:3] == ("node", str(tmp_path / "runtime.mjs"), str(tmp_path / envelope.path))
    flags = dict(zip(args[3::2], args[4::2], strict=True))
    assert flags == {
        "--workflow-run-id": "wf_42",
        "--session-id": "sess-7",
        "--name": "smoke",
        "--args": json.dumps({"naïve": "ünï", "deep": {"list": [1, None, True]}}),
    }
    kwargs = captured["kwargs"]
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["stdin"] == asyncio.subprocess.PIPE
    assert kwargs["stdout"] == asyncio.subprocess.PIPE
    assert kwargs["stderr"] == asyncio.subprocess.STDOUT


async def test_a_spawn_failure_reaches_the_caller(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)

    async def no_node(*args, **kwargs):
        raise FileNotFoundError("node")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_node)
    envelope = WorkflowMetaEnvelope(meta=WorkflowMeta(name="s", description="d"), path="w.mjs")

    with pytest.raises(FileNotFoundError):
        await runtime.start(
            WorkflowRunIdentity(workflow_run_id="wf_1", name="s"), envelope, session_id="s", args={}
        )

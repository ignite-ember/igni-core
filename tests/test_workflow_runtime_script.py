"""The Node bridge itself, run for real against small workflow scripts.

``workflow_runtime.mjs`` is what a workflow author's script runs inside:
its globals (``phase``, ``agent``, ``parallel``, ``pipeline``, ``log``),
its event stream, its exit codes and what it does on a cancel line or a
closed stdin. Every test here starts a real ``node`` and reads the JSON
lines it prints; skipped when Node is not on PATH.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from igni_core.backend.workflow_runtime_process import RUNTIME_PATH

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node not on PATH")

Event = dict[str, Any]
OnEvent = Callable[[Event, "_Node"], Awaitable[None]]


class _Node:
    """One run of the bridge: its events, its stderr, its exit code."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        self.events: list[Event] = []

    async def send(self, message: dict[str, Any] | str) -> None:
        line = message if isinstance(message, str) else json.dumps(message)
        assert self.proc.stdin is not None
        self.proc.stdin.write(line.encode() + b"\n")
        await self.proc.stdin.drain()

    def close_stdin(self) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.close()

    def types(self) -> list[str]:
        return [e["type"] for e in self.events]

    def of(self, type_: str) -> list[Event]:
        return [e for e in self.events if e["type"] == type_]


async def _start(script: Path, *flags: str) -> _Node:
    proc = await asyncio.create_subprocess_exec(
        "node",
        str(RUNTIME_PATH),
        str(script),
        *flags,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(script.parent),
    )
    return _Node(proc)


async def _finish(node: _Node, on_event: OnEvent | None) -> tuple[int, str]:
    async def read() -> None:
        assert node.proc.stdout is not None
        while line := await node.proc.stdout.readline():
            event = json.loads(line)
            node.events.append(event)
            if on_event is not None:
                await on_event(event, node)

    await asyncio.wait_for(read(), timeout=20)
    assert node.proc.stderr is not None
    stderr = (await node.proc.stderr.read()).decode()
    code = await asyncio.wait_for(node.proc.wait(), timeout=20)
    return code, stderr


async def run(
    tmp_path: Path,
    source: str,
    *flags: str,
    on_event: OnEvent | None = None,
    name: str = "wf.mjs",
) -> tuple[_Node, int, str]:
    script = tmp_path / name
    script.write_text(source)
    node = await _start(script, *flags)
    code, stderr = await _finish(node, on_event)
    return node, code, stderr


async def answer(event: Event, node: _Node) -> None:
    """Reply ``ok`` to every agent request with the prompt echoed back."""
    if event["type"] == "agent_request":
        payload = event["payload"]
        await node.send(
            {
                "type": "agent_response",
                "id": payload["id"],
                "ok": True,
                "result": f"echo:{payload['prompt']}",
            }
        )


# ── Discovery ──


async def test_discovery_prints_the_meta_and_exits_zero(tmp_path: Path) -> None:
    node, code, _ = await run(
        tmp_path,
        "export const meta = { name: 'wf', description: 'does it', phases: [] }\nawait 0\n",
        "--discovery",
    )

    assert code == 0
    [event] = node.events
    assert event["type"] == "workflow_meta"
    assert event["run_id"] == "wf_local"
    assert event["payload"]["meta"] == {"name": "wf", "description": "does it", "phases": []}
    assert event["payload"]["path"] == str(tmp_path / "wf.mjs")


async def test_discovery_without_a_meta_export_exits_three_with_a_message(tmp_path: Path) -> None:
    node, code, stderr = await run(tmp_path, "const x = 1\n", "--discovery")

    assert code == 3
    assert node.events == []
    assert "did not export 'meta'" in stderr


async def test_discovery_of_a_broken_script_exits_four(tmp_path: Path) -> None:
    node, code, stderr = await run(tmp_path, "export const meta = {\n", "--discovery")

    assert code == 4
    assert node.events == []
    assert "discovery failed" in stderr


async def test_discovery_does_not_see_a_meta_exported_after_an_await(tmp_path: Path) -> None:
    _, code, _ = await run(
        tmp_path, "await 0\nexport const meta = { name: 'late' }\n", "--discovery"
    )

    assert code == 3


async def test_discovery_reads_a_let_meta_and_ignores_names_that_merely_start_with_meta(
    tmp_path: Path,
) -> None:
    node, code, _ = await run(
        tmp_path,
        "const metadata = { name: 'wrong' }\nexport let meta = { name: 'right' }\n",
        "--discovery",
    )

    assert code == 0
    assert node.events[0]["payload"]["meta"] == {"name": "right"}


# ── The command line ──


async def test_a_missing_script_path_exits_two_with_a_message(tmp_path: Path) -> None:
    proc = await asyncio.create_subprocess_exec(
        "node",
        str(RUNTIME_PATH),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)

    assert proc.returncode == 2
    assert stdout == b""
    assert b"missing <workflow_path>" in stderr


async def test_workflow_started_carries_name_session_args_and_cwd(tmp_path: Path) -> None:
    node, code, _ = await run(
        tmp_path,
        "return args\n",
        "--workflow-run-id",
        "wf_9",
        "--session-id",
        "sess-1",
        "--name",
        "demo",
        "--args",
        json.dumps({"ünï": [1, {"deep": None}]}),
    )

    assert code == 0
    started = node.events[0]
    assert started["type"] == "workflow_started"
    assert started["run_id"] == "wf_9"
    assert started["payload"]["name"] == "demo"
    assert started["payload"]["session_id"] == "sess-1"
    assert started["payload"]["args"] == {"ünï": [1, {"deep": None}]}
    assert Path(started["payload"]["cwd"]).resolve() == tmp_path.resolve()
    # The script sees the same args as a global.
    assert node.events[-1]["payload"]["result"] == {"ünï": [1, {"deep": None}]}


async def test_without_flags_the_run_has_a_local_id_no_name_and_empty_args(
    tmp_path: Path,
) -> None:
    node, code, _ = await run(tmp_path, "return 1\n")

    assert code == 0
    started = node.events[0]
    assert started["run_id"] == "wf_local"
    assert started["payload"] == {
        "name": None,
        "session_id": "",
        "args": {},
        "cwd": started["payload"]["cwd"],
    }


async def test_events_are_numbered_from_zero_and_end_on_exactly_one_terminal(
    tmp_path: Path,
) -> None:
    node, code, _ = await run(tmp_path, "phase('a')\nlog('x')\nphase('b')\nlog('y')\nreturn 1\n")

    assert code == 0
    assert [e["seq"] for e in node.events] == list(range(len(node.events)))
    assert node.types()[0] == "workflow_started"
    terminals = [t for t in node.types() if t in {"workflow_completed", "workflow_failed"}]
    assert terminals == ["workflow_completed"]
    assert node.types()[-1] == "workflow_completed"
    assert all(isinstance(e["ts"], int) for e in node.events)


# ── The result ──


async def test_the_return_value_is_the_result_and_undefined_is_null(tmp_path: Path) -> None:
    node, code, _ = await run(tmp_path, "const v = await Promise.resolve({a: [1, 2]})\nreturn v\n")
    assert code == 0
    assert node.events[-1]["payload"]["result"] == {"a": [1, 2]}
    assert node.events[-1]["payload"]["status"] == "completed"

    node, code, _ = await run(tmp_path, "await 0\n")
    assert code == 0
    assert node.events[-1]["payload"]["result"] is None
    assert "duration_ms" in node.events[-1]["payload"]


# ── Failure ──


async def test_a_thrown_error_fails_the_run_closes_open_phases_and_exits_one(
    tmp_path: Path,
) -> None:
    node, code, _ = await run(tmp_path, "phase('doomed')\nthrow new Error('kaboom')\n")

    assert code == 1
    assert node.types() == [
        "workflow_started",
        "phase_started",
        "phase_completed",
        "workflow_failed",
    ]
    assert node.events[2]["payload"]["status"] == "failed"
    failed = node.events[3]["payload"]
    assert failed["error"] == "kaboom"
    assert "Error: kaboom" in failed["stack"]
    assert "duration_ms" in failed


async def test_malformed_args_end_the_run_with_a_clear_message_and_exit_two(
    tmp_path: Path,
) -> None:
    node, code, stderr = await run(tmp_path, "return 1\n", "--args", "{not json")

    assert code == 2
    assert node.events == []
    assert "--args is not valid JSON" in stderr


@pytest.mark.parametrize("raw", ["[1, 2]", "5", "null", '"text"'])
async def test_args_that_are_not_a_json_object_are_refused_with_exit_two(
    tmp_path: Path, raw: str
) -> None:
    node, code, stderr = await run(tmp_path, "return 1\n", "--args", raw)

    assert code == 2
    assert node.events == []
    assert "--args must be a JSON object" in stderr


async def test_args_given_as_a_bare_flag_are_refused_rather_than_read_as_true(
    tmp_path: Path,
) -> None:
    node, code, stderr = await run(tmp_path, "return args\n", "--args")

    assert code == 2
    assert node.events == []
    assert "--args must be a JSON object" in stderr

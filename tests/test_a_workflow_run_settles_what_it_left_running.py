"""A workflow run settles what it left running.

A run that ends while a call is still open leaves the window showing
that call as RUNNING for ever, and the last events of a busy run were
lost on the way out (campaign R664: the sibling lane of a failed group
read RUNNING under a FAILED card).

What we pin:

* a lane the failed group abandoned is settled — its call fails, and
  the window hears about it before the run's own end;
* a call the script left open is settled when the run ends;
* the last events of a run are not lost: the run's end is the last
  thing on stdout, however much the run printed.
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

    def types(self) -> list[str]:
        return [e["type"] for e in self.events]

    def of(self, type_: str) -> list[Event]:
        return [e for e in self.events if e["type"] == type_]


async def _run(
    tmp_path: Path, source: str, on_event: OnEvent | None = None
) -> tuple[_Node, int, str]:
    script = tmp_path / "wf.mjs"
    script.write_text(source)
    proc = await asyncio.create_subprocess_exec(
        "node",
        str(RUNTIME_PATH),
        str(script),
        "--workflow-run-id",
        "wf_1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(tmp_path),
    )
    node = _Node(proc)

    async def read() -> None:
        assert proc.stdout is not None
        while line := await proc.stdout.readline():
            event = json.loads(line)
            node.events.append(event)
            if on_event is not None:
                await on_event(event, node)

    await asyncio.wait_for(read(), timeout=30)
    assert proc.stderr is not None
    stderr = (await proc.stderr.read()).decode()
    code = await asyncio.wait_for(proc.wait(), timeout=30)
    return node, code, stderr


async def test_the_lane_a_failed_group_abandoned_is_not_left_running(tmp_path: Path) -> None:
    """The sibling lane of a failed group is settled, not left running.

    One lane's call is refused; the other lane's call is never
    answered. The group fails, and the abandoned lane must not stay
    RUNNING under the failed card.
    """

    async def refuse_the_first(event: Event, node: _Node) -> None:
        if event["type"] != "agent_request":
            return
        payload = event["payload"]
        if payload["prompt"] == "first":
            await node.send(
                {
                    "type": "agent_response",
                    "id": payload["id"],
                    "ok": False,
                    "error": "nobody can answer",
                }
            )

    node, code, _ = await _run(
        tmp_path,
        "await parallel([() => agent('first'), () => agent('second')])\n",
        on_event=refuse_the_first,
    )

    assert code == 1
    completed = {e["payload"]["id"]: e["payload"] for e in node.of("agent_completed")}
    assert len(completed) == 2, "the abandoned lane never reported"
    assert all(p["status"] == "failed" for p in completed.values())
    # The abandoned lane's failure is on the tape before the run ends,
    # so the window folds it in before it reads the run as finished.
    assert node.types().index("agent_completed") < node.types().index("workflow_failed")
    assert node.types()[-1] == "workflow_failed"
    # And the lane itself is reported: it was abandoned with the group.
    lanes = {e["payload"]["index"]: e["payload"] for e in node.of("parallel_lane_completed")}
    assert lanes[1]["status"] == "cancelled"
    assert lanes[0]["status"] == "failed"


async def test_a_call_the_script_left_open_is_settled_when_the_run_ends(tmp_path: Path) -> None:
    """A call nobody awaits is still a call the window knows about.

    The script starts a call and moves on. The run ends; the call must
    be settled with it, or the window shows it running for ever.
    """
    node, code, _ = await _run(
        tmp_path,
        "agent('never answered')\nreturn 'done'\n",
    )

    assert code == 0
    [completed] = node.of("agent_completed")
    assert completed["payload"]["status"] == "failed"
    assert completed["payload"]["error"]
    assert node.types()[-1] == "workflow_completed"
    assert node.events[-1]["payload"]["result"] == "done"


async def test_the_last_events_of_a_busy_run_are_not_lost(tmp_path: Path) -> None:
    """A run that printed a lot still ends on its own last event.

    The runtime used to call ``process.exit()``, which threw away
    whatever was still queued on the piped stdout: the run's own end
    was among the events that never arrived.
    """
    lines = 4000
    node, code, _ = await _run(
        tmp_path,
        f"for (let i = 0; i < {lines}; i++) {{ log('x'.repeat(100)) }}\nreturn 'done'\n",
    )

    assert code == 0
    assert len(node.of("log")) == lines
    assert node.types()[-1] == "workflow_completed"
    assert node.events[-1]["payload"]["result"] == "done"

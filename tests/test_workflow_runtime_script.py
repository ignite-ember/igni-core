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


# ── Phases and log ──


async def test_a_new_phase_closes_the_previous_one_and_every_phase_id_is_unique(
    tmp_path: Path,
) -> None:
    node, code, _ = await run(tmp_path, "phase('one')\nphase('two')\nphase('three')\n")

    assert code == 0
    assert node.types() == [
        "workflow_started",
        "phase_started",
        "phase_completed",
        "phase_started",
        "phase_completed",
        "phase_started",
        "phase_completed",  # the last, closed when the script ends
        "workflow_completed",
    ]
    started = [e["payload"] for e in node.of("phase_started")]
    assert [p["title"] for p in started] == ["one", "two", "three"]
    assert len({p["phase_id"] for p in started}) == 3
    completed = [e["payload"] for e in node.of("phase_completed")]
    assert [c["phase_id"] for c in completed] == [p["phase_id"] for p in started]
    assert {c["status"] for c in completed} == {"completed"}
    assert all(c["duration_ms"] >= 0 for c in completed)


async def test_log_and_console_log_become_log_events_inside_the_open_phase(
    tmp_path: Path,
) -> None:
    node, code, _ = await run(
        tmp_path,
        "log('before')\nphase('p')\nlog(42)\nconsole.log('a', {b: [1]}, 7, 'c')\n",
    )

    assert code == 0
    logs = node.of("log")
    assert [e["payload"]["text"] for e in logs] == ["before", "42", 'a {"b":[1]} 7 c']
    phase_id = node.of("phase_started")[0]["payload"]["phase_id"]
    assert [e["payload"]["phase_id"] for e in logs] == [None, phase_id, phase_id]


async def test_the_script_sandbox_has_no_process_or_fs(tmp_path: Path) -> None:
    node, code, _ = await run(
        tmp_path,
        "return [typeof process, typeof require, typeof setTimeout, typeof URL, "
        "typeof TextEncoder, typeof args]\n",
    )

    assert code == 0
    assert node.events[-1]["payload"]["result"] == [
        "undefined",
        "undefined",
        "function",
        "function",
        "function",
        "object",
    ]


# ── agent() ──


async def test_an_agent_call_emits_its_events_and_resolves_with_the_response(
    tmp_path: Path,
) -> None:
    node, code, _ = await run(
        tmp_path,
        "phase('p')\n"
        "const r = await agent('say hi', {label: 'greeter', schema: {a: 1, b: 2}})\n"
        "return r\n",
        on_event=answer,
    )

    assert code == 0
    assert node.types() == [
        "workflow_started",
        "phase_started",
        "agent_started",
        "agent_request",
        "agent_completed",
        "phase_completed",
        "workflow_completed",
    ]
    phase_id = node.of("phase_started")[0]["payload"]["phase_id"]
    started = node.of("agent_started")[0]["payload"]
    request = node.of("agent_request")[0]["payload"]
    completed = node.of("agent_completed")[0]["payload"]
    assert started["id"] == request["id"] == completed["id"]
    assert started["label"] == "greeter"
    assert started["phase_id"] == phase_id
    assert started["prompt_preview"] == "say hi"
    assert started["schema_keys"] == ["a", "b"]
    assert request == {
        "id": request["id"],
        "prompt": "say hi",
        "label": "greeter",
        "phase": None,
        "timeout_seconds": 600,
    }
    assert completed["status"] == "completed"
    assert node.events[-1]["payload"]["result"] == "echo:say hi"


async def test_an_agent_call_without_options_is_labelled_agent_with_no_schema(
    tmp_path: Path,
) -> None:
    node, code, _ = await run(tmp_path, "return await agent('p')\n", on_event=answer)

    assert code == 0
    started = node.of("agent_started")[0]["payload"]
    assert started["label"] == "agent"
    assert started["schema_keys"] is None
    assert started["phase_id"] is None


async def test_the_prompt_preview_is_cut_at_240_characters_and_empty_for_a_non_string(
    tmp_path: Path,
) -> None:
    node, code, _ = await run(
        tmp_path,
        "const long = 'é'.repeat(300)\n"
        "await agent(long)\n"
        "await agent({structured: true}, {schema: 'not an object'})\n",
        on_event=answer,
    )

    assert code == 0
    first, second = (e["payload"] for e in node.of("agent_started"))
    assert first["prompt_preview"] == "é" * 240
    assert second["prompt_preview"] == ""
    assert second["schema_keys"] is None
    # The request itself carries the whole prompt, structured as given.
    first_request, second_request = (e["payload"] for e in node.of("agent_request"))
    assert first_request["prompt"] == "é" * 300
    assert second_request["prompt"] == {"structured": True}


async def test_a_failed_agent_response_rejects_the_call_with_its_error(tmp_path: Path) -> None:
    async def refuse(event: Event, node: _Node) -> None:
        if event["type"] == "agent_request":
            await node.send(
                {"type": "agent_response", "id": event["payload"]["id"], "ok": False, "error": "no"}
            )

    node, code, _ = await run(
        tmp_path,
        "try { await agent('x') } catch (e) { return 'caught ' + e.message }\n",
        on_event=refuse,
    )

    assert code == 0
    completed = node.of("agent_completed")[0]["payload"]
    assert completed["status"] == "failed"
    assert completed["error"] == "no"
    assert node.events[-1]["payload"]["result"] == "caught no"


async def test_a_failed_response_without_an_error_says_agent_call_failed(tmp_path: Path) -> None:
    async def refuse(event: Event, node: _Node) -> None:
        if event["type"] == "agent_request":
            await node.send({"type": "agent_response", "id": event["payload"]["id"], "ok": False})

    node, code, _ = await run(
        tmp_path, "try { await agent('x') } catch (e) { return e.message }\n", on_event=refuse
    )

    assert code == 0
    assert node.events[-1]["payload"]["result"] == "agent call failed"


async def test_a_response_for_an_unknown_id_and_junk_lines_are_ignored(tmp_path: Path) -> None:
    async def noisy(event: Event, node: _Node) -> None:
        if event["type"] == "agent_request":
            await node.send({"type": "agent_response", "id": "req_other", "ok": True, "result": 1})
            await node.send("this is not json")
            await node.send({"type": "mystery"})
            await answer(event, node)

    node, code, _ = await run(tmp_path, "return await agent('q')\n", on_event=noisy)

    assert code == 0
    assert node.events[-1]["payload"]["result"] == "echo:q"


async def test_a_response_split_across_writes_is_buffered_until_the_line_is_whole(
    tmp_path: Path,
) -> None:
    async def split(event: Event, node: _Node) -> None:
        if event["type"] == "agent_request":
            line = json.dumps(
                {"type": "agent_response", "id": event["payload"]["id"], "ok": True, "result": "ü"}
            )
            node.proc.stdin.write(line[:20].encode())
            await node.proc.stdin.drain()
            await asyncio.sleep(0.1)
            node.proc.stdin.write(line[20:].encode("utf-8") + b"\n")
            await node.proc.stdin.drain()

    node, code, _ = await run(tmp_path, "return await agent('q')\n", on_event=split)

    assert code == 0
    assert node.events[-1]["payload"]["result"] == "ü"


async def test_an_agent_call_that_times_out_emits_timeout_and_rejects(tmp_path: Path) -> None:
    node, code, _ = await run(
        tmp_path,
        "try { await agent('slow', {label: 'slowpoke', timeoutSeconds: 0.2}) }\n"
        "catch (e) { return e.message }\n",
    )

    assert code == 0
    completed = node.of("agent_completed")[0]["payload"]
    assert completed["status"] == "timeout"
    assert completed["label"] == "slowpoke"
    assert completed["error"] == "agent timed out after 0.2s"
    assert node.of("agent_request")[0]["payload"]["timeout_seconds"] == 0.2
    assert node.events[-1]["payload"]["result"] == "agent 'slowpoke' timed out after 0.2s"


@pytest.mark.parametrize("timeout", [0, -5])
async def test_a_zero_or_negative_timeout_means_the_call_waits_for_its_answer(
    tmp_path: Path, timeout: int
) -> None:
    async def late(event: Event, node: _Node) -> None:
        if event["type"] == "agent_request":
            await asyncio.sleep(0.5)
            await answer(event, node)

    node, code, _ = await run(
        tmp_path, f"return await agent('q', {{timeoutSeconds: {timeout}}})\n", on_event=late
    )

    assert code == 0
    assert node.events[-1]["payload"]["result"] == "echo:q"


async def test_a_closed_stdin_rejects_pending_agent_calls(tmp_path: Path) -> None:
    async def hang_up(event: Event, node: _Node) -> None:
        if event["type"] == "agent_request":
            node.close_stdin()

    node, code, _ = await run(
        tmp_path,
        "try { await agent('x') } catch (e) { return e.message }\n",
        on_event=hang_up,
    )

    assert code == 0
    completed = node.of("agent_completed")[0]["payload"]
    assert completed["status"] == "failed"
    assert completed["error"] == "subprocess stdin closed"
    assert node.events[-1]["payload"]["result"] == "subprocess stdin closed"


# ── cancel ──


async def cancel_on_request(event: Event, node: _Node) -> None:
    if event["type"] == "agent_request":
        await node.send({"type": "cancel"})


async def test_a_cancel_line_rejects_the_pending_agent_and_a_script_that_returns_ends_cancelled(
    tmp_path: Path,
) -> None:
    node, code, _ = await run(
        tmp_path,
        "phase('work')\n"
        "try { await agent('x') } catch (e) { log('caught ' + e.message) }\n"
        "return 'ignored result'\n",
        on_event=cancel_on_request,
    )

    assert code == 0
    assert node.of("agent_completed")[0]["payload"]["status"] == "failed"
    assert node.of("agent_completed")[0]["payload"]["error"] == "workflow cancelled"
    assert node.of("log")[0]["payload"]["text"] == "caught workflow cancelled"
    assert node.of("phase_completed")[0]["payload"]["status"] == "cancelled"
    final = node.events[-1]
    assert final["type"] == "workflow_completed"
    assert final["payload"]["status"] == "cancelled"
    assert "result" not in final["payload"]
    assert node.types().count("workflow_failed") == 0


async def test_a_cancel_that_rejects_an_uncaught_agent_call_ends_cancelled_not_failed(
    tmp_path: Path,
) -> None:
    node, code, _ = await run(
        tmp_path,
        "phase('work')\nawait agent('x')\nreturn 'never'\n",
        on_event=cancel_on_request,
    )

    assert code == 0
    assert node.of("phase_completed")[0]["payload"]["status"] == "cancelled"
    assert node.types()[-1] == "workflow_completed"
    assert node.events[-1]["payload"]["status"] == "cancelled"
    assert "workflow_failed" not in node.types()


async def test_a_cancel_with_no_agent_pending_marks_the_next_phase_close_cancelled(
    tmp_path: Path,
) -> None:
    async def cancel_on_log(event: Event, node: _Node) -> None:
        if event["type"] == "log":
            await node.send({"type": "cancel"})

    node, code, _ = await run(
        tmp_path,
        "phase('a')\nlog('go')\nawait new Promise((r) => setTimeout(r, 300))\n"
        "phase('b')\nreturn 1\n",
        on_event=cancel_on_log,
    )

    assert code == 0
    statuses = [e["payload"]["status"] for e in node.of("phase_completed")]
    assert statuses == ["cancelled", "cancelled"]
    assert node.events[-1]["payload"]["status"] == "cancelled"

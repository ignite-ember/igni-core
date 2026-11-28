"""One cell of the matrix: put a fresh session in a state, act, finish, and check everything."""

from __future__ import annotations

import asyncio
import contextlib
import time

from tests.session_lifecycle.enums.matrix_action import MatrixAction
from tests.session_lifecycle.enums.matrix_state import MatrixState
from tests.session_lifecycle.invariant_checker import InvariantChecker
from tests.session_lifecycle.lifecycle_client import LifecycleClient
from tests.session_lifecycle.lifecycle_rig import LifecycleRig
from tests.session_lifecycle.schemas.model_turn import ModelTurn
from tests.session_lifecycle.schemas.scripted_tool_call import ScriptedToolCall
from tests.session_lifecycle.schemas.violation import Violation

LONG_TEXT = "Here is a long answer that keeps streaming while the person acts. " * 4


class MatrixCell:
    """One state × action, run end to end against the real backend."""

    # The most one cell may take before it counts as hung.
    LIMIT_SECONDS = 120.0

    def __init__(self, rig: LifecycleRig, state: MatrixState, action: MatrixAction) -> None:
        self.rig = rig
        self.state = state
        self.action = action
        self.cell_id = f"{state.value}.{action.value}"
        self.m = f"[cell {self.cell_id}]"
        self.checker = InvariantChecker(rig, self.m)
        self.checker.side_markers.append(f"subtask {self.m}")
        self.client: LifecycleClient | None = None
        self.extra_windows: list[LifecycleClient] = []
        self.gates: list[str] = []
        self.left_sessions: list[str] = []
        self.step = "start"
        self.expected_model = rig.model.model_id

    # ── The run ──────────────────────────────────────────────────

    async def run(self) -> list[Violation]:
        self._script()
        try:
            await asyncio.wait_for(self._scenario(), self.LIMIT_SECONDS)
        except Exception as exc:  # noqa: BLE001 — a stuck scenario is a finding
            self.checker.fail("scenario", self.step, f"{type(exc).__name__}: {exc}")
        finally:
            for gate in self.gates:
                self.rig.model.release(gate)
            if self.action is MatrixAction.SWITCH_MODEL and self.client is not None:
                # A switch is saved as the default for new chats: put it back for the next cell.
                with contextlib.suppress(Exception):
                    await self.client.switch_model("scripted")
            for window in [*self.extra_windows, *([self.client] if self.client else [])]:
                with contextlib.suppress(Exception):
                    await window.close()
                if window in self.rig.windows:
                    self.rig.windows.remove(window)
        return self.checker.violations

    async def _scenario(self) -> None:
        self.step = "connect"
        self.client = await self.rig.new_session(f"w{self.cell_id}")
        self.step = f"enter {self.state.value}"
        await self._enter()
        self.checker.check_requests(self.step)
        self.step = f"{self.action.value} in {self.state.value}"
        await self._act()
        self.checker.check_requests(self.step)
        self.checker.check_zombies(self.step, self.client)
        self.step = "finish"
        await self._finish()
        self.step = "probe"
        await self._probe()

    # ── The script ───────────────────────────────────────────────

    def _gate(self, name: str) -> str:
        gate = f"{name} {self.m}"
        self.gates.append(gate)
        self.rig.model.gate(gate)
        return gate

    def _script(self) -> None:
        m = self.m
        shell = "run_shell_command"
        script = self.rig.script
        script.add(f"warmup {m}", ModelTurn(text="A warm reply."))
        script.add(f"stream {m}", ModelTurn(text=LONG_TEXT, hold=self._gate("stream")))
        script.add(
            f"tool {m}",
            ModelTurn(
                text="Sleeping.",
                tool_calls=[ScriptedToolCall(name=shell, arguments={"command": "sleep 3"})],
            ),
            ModelTurn(text="Slept."),
        )
        script.add(
            f"pause {m}",
            ModelTurn(
                text="I need to write a file.",
                tool_calls=[
                    ScriptedToolCall(
                        name=shell, arguments={"command": "echo approved > approved.txt"}
                    )
                ],
            ),
            ModelTurn(text="Wrote it."),
        )
        script.add(
            f"continue {m}",
            ModelTurn(
                text="I need to write a file.",
                tool_calls=[
                    ScriptedToolCall(
                        name=shell, arguments={"command": "echo continued > continued.txt"}
                    )
                ],
            ),
            ModelTurn(text=LONG_TEXT, hold=self._gate("continue")),
        )
        script.add(
            f"delegate {m}",
            ModelTurn(
                text="Delegating.",
                tool_calls=[
                    ScriptedToolCall(
                        name="spawn_agent",
                        arguments={"task": f"subtask {m}: look around", "agent_name": "explorer"},
                    )
                ],
            ),
            ModelTurn(text="The sub-agent is done."),
        )
        script.add(
            f"subtask {m}",
            ModelTurn(text="Sub-agent looking around. " * 3, hold=self._gate("subtask")),
            ModelTurn(text="Sub-agent result."),
        )
        script.add(
            f"background {m}",
            ModelTurn(
                text="Starting a process.",
                tool_calls=[
                    ScriptedToolCall(
                        name=shell, arguments={"command": "sleep 20", "background": True}
                    )
                ],
            ),
            ModelTurn(text="It is running in the background."),
        )
        failing = ModelTurn(error_status=500)
        script.add(f"fail {m}", failing, then=failing)
        for word in ("followup-one", "followup-two", "feedback", "probe"):
            script.add(f"{word} {m}", ModelTurn(text=f"Answering {word}."))

    # ── Entering a state ─────────────────────────────────────────

    async def _send(self, word: str) -> str:
        assert self.client is not None
        text = f"{word} {self.m}"
        self.checker.expect(text)
        return await self.client.send(text)

    async def _wait_stream_end(self, request_id: str, timeout: float = 20.0) -> None:
        assert self.client is not None
        await self.client.wait_for(
            lambda f: f.type == "stream_end" and f.id == request_id, since=0, timeout=timeout
        )

    def _streaming(self, rule_word: str, min_step: int = 0) -> None:
        self.rig.model.wait_for(
            lambda rs: any(
                r.rule == f"{rule_word} {self.m}"
                and r.step >= min_step
                and r.outcome == "streaming"
                for r in rs
            ),
            timeout=20,
        )

    async def _enter(self) -> None:
        client = self.client
        assert client is not None
        state = self.state
        if state in (MatrixState.IDLE, MatrixState.RESTARTED):
            await self._wait_stream_end(await self._send("warmup"))
            self.checker.mark("completed")
            await client.quiet()
            if state is MatrixState.RESTARTED:
                await self._restart()
        elif state in (MatrixState.STREAMING, MatrixState.CANCELLED):
            await self._send("stream")
            await asyncio.to_thread(self._streaming, "stream")
            await client.wait_for(
                lambda f: f.type == "content_delta" and f.session_id == client.session_id, since=0
            )
            if state is MatrixState.CANCELLED:
                await client.stop()
                self.checker.mark("cancelled")
                self.rig.model.release(f"stream {self.m}")
                await asyncio.sleep(1.0)
                await client.quiet()
        elif state is MatrixState.TOOL:
            await self._send("tool")
            await client.wait_for(
                lambda f: f.type == "tool_started" and f.session_id == client.session_id, since=0
            )
        elif state in (MatrixState.PAUSED, MatrixState.CONTINUING):
            word = "pause" if state is MatrixState.PAUSED else "continue"
            await self._send(word)
            await client.wait_for(
                lambda f: f.type == "run_paused" and f.session_id == client.session_id, since=0
            )
            await client.quiet()
            self.checker.mark("paused")
            if state is MatrixState.CONTINUING:
                await client.answer("confirm", "once")
                await asyncio.to_thread(self._streaming, "continue", 1)
                self.checker.mark("sent")
        elif state is MatrixState.SUBAGENT:
            await self._send("delegate")
            await asyncio.to_thread(self._streaming, "subtask")
        elif state is MatrixState.BACKGROUND:
            await self._wait_stream_end(await self._send("background"))
            self.checker.mark("completed")
            await client.quiet()
        elif state is MatrixState.ERRORED:
            await self._wait_stream_end(await self._send("fail"), timeout=60)
            self.checker.mark("errored")
            await client.quiet()

    async def _restart(self) -> None:
        assert self.client is not None
        await asyncio.to_thread(self.rig.restart)
        await self.client.reconnect()

    # ── The action ───────────────────────────────────────────────

    async def _follow_up(self, word: str) -> None:
        assert self.client is not None
        text = f"{word} {self.m}"
        kind = "queued" if self.client.view.queues_messages else "message"
        self.checker.expect(text, kind)
        await self.client.follow_up(text)

    async def _cancel(self, times: int) -> None:
        client = self.client
        assert client is not None
        mark = len(client.frames)
        cancelled_at = time.monotonic()
        was_working = self.state not in (
            MatrixState.IDLE,
            MatrixState.RESTARTED,
            MatrixState.ERRORED,
            MatrixState.CANCELLED,
            MatrixState.BACKGROUND,
        )
        for _ in range(times):
            await client.stop()
        if was_working:
            self.checker.mark("cancelled")
        await self.checker.check_cancelled(self.step, client, cancelled_at, mark)

    def _stale_ids(self) -> list[str]:
        assert self.client is not None
        seen = [
            str(req.get("requirement_id"))
            for f in self.client.frames
            if f.type == "run_paused"
            for req in f.data.get("requirements") or []
        ]
        return seen or ["stale-requirement"]

    async def _act(self) -> None:
        client = self.client
        assert client is not None
        action = self.action
        if action is MatrixAction.FOLLOW_UP:
            await self._follow_up("followup-one")
        elif action is MatrixAction.TWO_FOLLOW_UPS:
            await self._follow_up("followup-one")
            await self._follow_up("followup-two")
        elif action is MatrixAction.CANCEL:
            await self._cancel(1)
        elif action is MatrixAction.CANCEL_TWICE:
            await self._cancel(2)
        elif action in (MatrixAction.APPROVE, MatrixAction.DENY, MatrixAction.DENY_FEEDBACK):
            verb = "confirm" if action is MatrixAction.APPROVE else "reject"
            ids = None if client.view.pending else self._stale_ids()
            if verb == "reject" and client.view.pending:
                self.checker.mark("denied")
            await client.answer(verb, "once", requirement_ids=ids)
            if action is MatrixAction.DENY_FEEDBACK:
                await asyncio.sleep(0.2)
                await self._follow_up("feedback")
        elif action is MatrixAction.SWITCH_MODEL:
            reply = await client.switch_model("scripted-alt")
            if reply.type == "error":
                self.checker.fail(
                    "model-switch", self.step, f"the switch was refused: {reply.text}"
                )
            else:
                self.expected_model = f"{self.rig.model.model_id}-alt"
        elif action is MatrixAction.RECONNECT:
            await client.reconnect()
        elif action is MatrixAction.RELOAD:
            await client.reload()
        elif action is MatrixAction.SECOND_WINDOW:
            await self._second_window()
        elif action is MatrixAction.FORK:
            await self._fork()
        elif action is MatrixAction.CLEAR:
            await self._clear()
        elif action is MatrixAction.RESTART:
            await self._restart()
        await asyncio.sleep(0.3)
        await client.quiet(0.5, limit=10)

    async def _second_window(self) -> None:
        assert self.client is not None
        other = await self.rig.window(f"w2{self.cell_id}")
        self.extra_windows.append(other)
        await other.attach(self.client.session_id)
        await asyncio.sleep(0.5)
        await other.quiet(0.4, limit=5)
        on_disk = sorted(self.rig.db.waiting_approvals(self.client.session_id))
        shown = sorted(p.requirement_id for p in other.view.pending)
        if shown != on_disk:
            self.checker.fail(
                "stop-affordance",
                self.step,
                f"a second window shows approvals {shown}; the session waits on {on_disk}",
            )

    async def _fork(self) -> None:
        client = self.client
        assert client is not None
        reply = await client.command("/fork")
        new_id = str(reply.data.get("content") or "").strip()
        if reply.type == "command_result" and "Nothing to fork yet" in new_id:
            return  # A chat not saved yet (its first turn still runs) has nothing to fork: said so.
        if reply.type == "error" or not new_id or " " in new_id:
            self.checker.fail(
                "fork",
                self.step,
                f"/fork answered {reply.type}: {reply.data.get('content') or reply.text}",
            )
            return
        self.left_sessions.append(client.session_id)
        await client.attach(new_id)

    async def _clear(self) -> None:
        client = self.client
        assert client is not None
        reply = await client.command("/clear")
        if reply.type == "error":
            self.checker.fail("clear", self.step, f"/clear answered error: {reply.text}")
            return
        new_id = str(await client.rpc("get_session_id") or "")
        if new_id and new_id != client.session_id:
            self.left_sessions.append(client.session_id)
            client.session_id = new_id
            client.view.rebind(new_id)
        self.checker.cleared()

    # ── Finishing ────────────────────────────────────────────────

    async def _finish(self) -> None:
        client = self.client
        assert client is not None
        for gate in self.gates:
            self.rig.model.release(gate)
        await asyncio.sleep(0.3)
        await client.quiet(0.6, limit=20)
        state, waiting = await self.checker.backend_state(client)
        if waiting:
            if not client.view.pending:
                await client.attach(client.session_id)
                await client.quiet(0.4, limit=5)
            await client.answer("reject", "once")
            await asyncio.sleep(0.3)
            await client.quiet(0.6, limit=20)
        if not await self._goes_idle(client):
            self.checker.fail("goes-idle", self.step, "the session never went idle by itself")
            await client.stop()
            await self._goes_idle(client, 5)
        self.checker.check_requests(self.step)
        self.checker.check_zombies(self.step, client)
        await self.checker.check_settled(self.step, client)

    async def _goes_idle(self, client: LifecycleClient, timeout: float = 12.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state, waiting = await self.checker.backend_state(client)
            if state == "idle" and not waiting and not await client.processing():
                return True
            await asyncio.sleep(0.25)
        return False

    async def _probe(self) -> None:
        client = self.client
        assert client is not None
        request_id = await self._send("probe")
        try:
            await self._wait_stream_end(request_id, timeout=25)
        except TimeoutError:
            self.checker.fail("answers", self.step, "the next message was never answered")
            return
        probes = [r for r in self.rig.model.agent_requests() if r.rule == f"probe {self.m}"]
        if not probes:
            self.checker.fail("answers", self.step, "the next message never reached the model")
        elif probes[-1].model != self.expected_model:
            self.checker.fail(
                "model-switch",
                self.step,
                f"the next run asked {probes[-1].model!r}, not {self.expected_model!r}",
            )
        await client.quiet()
        self.checker.check_requests(self.step)
        self.checker.check_stream_ends(self.step, client)
        self.checker.check_zombies(self.step, client)
        self.checker.check_persisted(self.step, client)
        if self.action not in (MatrixAction.FORK, MatrixAction.CLEAR):
            self.checker.check_tokens(self.step, client)
        await self.checker.check_settled(self.step, client)
        for left in self.left_sessions:
            states = await client.rpc("list_session_states")
            for entry in (states or {}).get("sessions") or []:
                if entry.get("session_id") == left and entry.get("state") == "running":
                    self.checker.fail(
                        "goes-idle", self.step, f"the session left behind ({left}) still runs"
                    )

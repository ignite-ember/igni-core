"""What the terminal tests share: a recording transport, a calm bash, a scripted PTY."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from igni_core.backend.pool_replier import PoolReplier
from igni_core.backend.pool_rpcs import PoolRpcRouter, TerminalRpc
from igni_core.backend.terminal.terminal_process import (
    ExitCallback,
    OutputCallback,
    TerminalProcess,
)
from igni_core.backend.terminal.terminal_registry import Spawn, TerminalRegistry
from igni_core.core.config.permission_eval.mode_policy import ModePolicy
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod


class Wire:
    """Every message igni sent, with the connection it was addressed to."""

    def __init__(self) -> None:
        self.sent: list[msg.Message] = []

    async def send(self, message: msg.Message) -> None:
        self.sent.append(message)

    def pushes(self, channel: str, terminal_id: str | None = None) -> list[msg.PushNotification]:
        return [
            m
            for m in self.sent
            if isinstance(m, msg.PushNotification)
            and m.channel == channel
            and (terminal_id is None or m.payload.get("terminal_id") == terminal_id)
        ]

    def output(self, terminal_id: str) -> str:
        return "".join(str(p.payload["data"]) for p in self.pushes("terminal_output", terminal_id))

    def responses(self, request_id: str) -> list[msg.RPCResponse]:
        return [m for m in self.sent if isinstance(m, msg.RPCResponse) and m.id == request_id]

    async def wait_for_output(self, terminal_id: str, text: str, timeout: float = 15.0) -> str:
        """The terminal's output once ``text`` is in it; fails after ``timeout``."""
        await wait_until(lambda: text in self.output(terminal_id), timeout)
        return self.output(terminal_id)


async def wait_until(condition: Callable[[], bool], timeout: float = 15.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting")
        await asyncio.sleep(0.02)


def calm_bash(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``$SHELL`` is bash with a plain prompt and no greeting, in its own home."""
    home.mkdir(parents=True, exist_ok=True)
    (home / ".bash_profile").write_text("PS1='$ '\nunset PROMPT_COMMAND\nset +H\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setenv("BASH_SILENCE_DEPRECATION_WARNING", "1")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Rig:
    """A router answering the terminal calls over a recording transport."""

    def __init__(
        self,
        cwd: Path,
        *,
        policy: ModePolicy | None = None,
        spawn: Spawn | None = None,
        audit: Any = None,
    ) -> None:
        self.wire = Wire()
        self.policy = policy
        if spawn is None:
            self.terminals = TerminalRegistry(send=self.wire.send, cwd=cwd, audit=audit)
        else:
            self.terminals = TerminalRegistry(
                send=self.wire.send, cwd=cwd, audit=audit, spawn=spawn
            )
        self.router = PoolRpcRouter(PoolReplier(self.wire))  # type: ignore[arg-type]
        self.router.mount(TerminalRpc(self.terminals, policy=lambda: self.policy))
        self._ids = 0

    async def call(
        self, method: RpcMethod, args: dict[str, Any], client: str = "window-1"
    ) -> msg.RPCResponse:
        self._ids += 1
        request = msg.RPCRequest(id=f"r{self._ids}", method=method.value, args=args)
        if client:
            request.mark_from(client)
        await self.router.handle(request)
        (response,) = self.wire.responses(request.id)
        return response

    async def open(self, client: str = "window-1", cols: int = 80, rows: int = 24) -> str:
        response = await self.call(RpcMethod.TERMINAL_OPEN, {"cols": cols, "rows": rows}, client)
        assert response.error is None, response.error
        assert isinstance(response.result, dict)
        return str(response.result["terminal_id"])

    async def type(self, terminal_id: str, data: str, client: str = "window-1") -> None:
        response = await self.call(
            RpcMethod.TERMINAL_INPUT, {"terminal_id": terminal_id, "data": data}, client
        )
        assert response.error is None, response.error


class ScriptedPty(TerminalProcess):
    """A PTY the test drives: it records what it is told and prints what it is handed."""

    def __init__(self, cwd: Path, cols: int, rows: int) -> None:
        self.cwd = cwd
        self.size = (cols, rows)
        self.typed = bytearray()
        self.paused = False
        self.pauses = 0
        self.resumes = 0
        self.closed = 0
        self.on_output: OutputCallback | None = None
        self.on_exit: ExitCallback | None = None

    @property
    def shell(self) -> str:
        return "/bin/scripted"

    def start(
        self, loop: asyncio.AbstractEventLoop, on_output: OutputCallback, on_exit: ExitCallback
    ) -> None:
        self.on_output = on_output
        self.on_exit = on_exit

    def write(self, data: bytes) -> None:
        self.typed += data

    def resize(self, cols: int, rows: int) -> None:
        self.size = (cols, rows)

    def pause_reading(self) -> None:
        self.paused = True
        self.pauses += 1

    def resume_reading(self) -> None:
        self.paused = False
        self.resumes += 1

    async def close(self) -> None:
        self.closed += 1
        self.exit(-1)

    def prints(self, data: bytes) -> None:
        assert self.on_output is not None
        self.on_output(data)

    def exit(self, code: int | None) -> None:
        if self.on_exit is not None:
            on_exit, self.on_exit = self.on_exit, None
            on_exit(code)


class ScriptedSpawn:
    """Spawns :class:`ScriptedPty` and keeps every one it made."""

    def __init__(self) -> None:
        self.made: list[ScriptedPty] = []

    def __call__(self, cwd: Path, cols: int, rows: int) -> TerminalProcess:
        pty = ScriptedPty(cwd, cols, rows)
        self.made.append(pty)
        return pty

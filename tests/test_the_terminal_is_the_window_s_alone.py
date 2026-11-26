"""A terminal is the window's that opened it, and only igni's window opens one.

Another connection naming its id is told it is not found, and nothing is
done to it. A program is refused before any handler runs. Eight per
connection; a ninth is refused. With no connection id or no PTY support the
call is unavailable, and a shell that cannot start fails.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from igni_core.backend import standing_policy
from igni_core.backend.pool_replier import PoolReplier
from igni_core.backend.pool_rpcs import POOL_RPCS, PoolRpcRouter, TerminalRpc
from igni_core.backend.rpc_handlers.pool_guard import PAGE_RPCS, PLATFORM_RPCS, POOL_LEVEL_RPCS
from igni_core.backend.terminal.exceptions import TerminalUnavailable
from igni_core.backend.terminal.terminal_process import TerminalProcess
from igni_core.backend.terminal.terminal_registry import TerminalRegistry
from igni_core.extensions.host.coverage import PUSH_UNREACHABLE, RPC_UNREACHABLE
from igni_core.protocol import messages as msg
from igni_core.protocol import rpc_contract
from igni_core.protocol.rpc import RpcMethod
from igni_core.protocol.schemas.enums import PushChannel
from igni_core.protocol.schemas.envelope import Standing
from igni_core.protocol.schemas.rpc import RpcErrorCode
from tests.terminal_helpers import Rig, ScriptedSpawn, Wire, calm_bash, wait_until

TERMINAL_RPCS = (
    RpcMethod.TERMINAL_OPEN,
    RpcMethod.TERMINAL_INPUT,
    RpcMethod.TERMINAL_RESIZE,
    RpcMethod.TERMINAL_CLOSE,
)


@pytest.fixture
def spawn() -> ScriptedSpawn:
    return ScriptedSpawn()


@pytest.fixture
async def rig(tmp_path: Path, spawn: ScriptedSpawn):
    rig = Rig(tmp_path, spawn=spawn)
    yield rig
    await rig.terminals.close_all()


# ── another connection ─────────────────────────────────────────────


async def test_another_window_cannot_type_into_resize_or_close_it(rig, spawn):
    terminal_id = await rig.open(client="window-1")
    pty = spawn.made[0]

    typed = await rig.call(
        RpcMethod.TERMINAL_INPUT, {"terminal_id": terminal_id, "data": "rm -rf ~\r"}, "window-2"
    )
    resized = await rig.call(
        RpcMethod.TERMINAL_RESIZE, {"terminal_id": terminal_id, "cols": 9, "rows": 9}, "window-2"
    )
    closed = await rig.call(RpcMethod.TERMINAL_CLOSE, {"terminal_id": terminal_id}, "window-2")

    for response in (typed, resized, closed):
        assert response.code is RpcErrorCode.NOT_FOUND
        assert terminal_id in (response.error or "")
    assert pty.typed == b""
    assert pty.size == (80, 24)
    assert pty.closed == 0
    assert rig.terminals.count("window-1") == 1
    # Its owner still reaches it.
    await rig.type(terminal_id, "ls\r", client="window-1")
    assert pty.typed == b"ls\r"


async def test_an_unknown_id_is_not_found(rig):
    for method, args in (
        (RpcMethod.TERMINAL_INPUT, {"terminal_id": "term-nope", "data": "x"}),
        (RpcMethod.TERMINAL_RESIZE, {"terminal_id": "term-nope", "cols": 2, "rows": 2}),
        (RpcMethod.TERMINAL_CLOSE, {"terminal_id": "term-nope"}),
    ):
        response = await rig.call(method, args)
        assert response.code is RpcErrorCode.NOT_FOUND
        assert (
            response.error == f"{method.value}: no terminal 'term-nope' is open on this connection"
        )


async def test_input_and_resize_reach_the_owner_s_pty(rig, spawn, tmp_path):
    terminal_id = await rig.open(cols=100, rows=30)
    pty = spawn.made[0]
    assert pty.size == (100, 30)
    assert pty.cwd == tmp_path

    await rig.type(terminal_id, "héllo\r")
    resized = await rig.call(
        RpcMethod.TERMINAL_RESIZE, {"terminal_id": terminal_id, "cols": 120, "rows": 50}
    )

    assert pty.typed == "héllo\r".encode()
    assert pty.size == (120, 50)
    assert resized.result == {"terminal_id": terminal_id}


async def test_an_extra_argument_is_invalid(rig, spawn):
    response = await rig.call(RpcMethod.TERMINAL_OPEN, {"cols": 80, "rows": 24, "shell": "/bin/sh"})
    assert response.code is RpcErrorCode.INVALID
    assert spawn.made == []


# ── how many ───────────────────────────────────────────────────────


async def test_a_window_may_have_eight_and_a_ninth_is_refused(rig, spawn):
    ids = [await rig.open() for _ in range(TerminalRegistry.MAX_PER_CONNECTION)]
    assert len(set(ids)) == 8

    ninth = await rig.call(RpcMethod.TERMINAL_OPEN, {"cols": 80, "rows": 24})

    assert ninth.code is RpcErrorCode.REFUSED
    assert ninth.error == (
        "terminal_open: a window may have at most 8 terminals open; close one first"
    )
    assert len(spawn.made) == 8
    # Another window has its own eight.
    await rig.open(client="window-2")
    # Closing one makes room.
    await rig.call(RpcMethod.TERMINAL_CLOSE, {"terminal_id": ids[0]})
    await rig.open()
    assert rig.terminals.count("window-1") == 8


# ── what cannot be answered ────────────────────────────────────────


async def test_a_connection_with_no_id_cannot_have_a_terminal(rig, spawn):
    response = await rig.call(RpcMethod.TERMINAL_OPEN, {"cols": 80, "rows": 24}, client="")

    assert response.code is RpcErrorCode.UNAVAILABLE
    assert response.error == f"terminal_open: {TerminalRpc.NO_CONNECTION}"
    assert spawn.made == []


async def test_a_machine_without_pty_support_says_so(tmp_path):
    def no_pty(cwd: Path, cols: int, rows: int) -> TerminalProcess:
        raise TerminalUnavailable("the terminal needs pywinpty on Windows, and it is not installed")

    rig = Rig(tmp_path, spawn=no_pty)
    response = await rig.call(RpcMethod.TERMINAL_OPEN, {"cols": 80, "rows": 24})

    assert response.code is RpcErrorCode.UNAVAILABLE
    assert response.error == (
        "terminal_open: the terminal needs pywinpty on Windows, and it is not installed"
    )
    assert rig.terminals.count("window-1") == 0


async def test_a_shell_that_cannot_start_fails_and_leaves_nothing_open(tmp_path):
    def broken(cwd: Path, cols: int, rows: int) -> TerminalProcess:
        raise FileNotFoundError(2, "No such file or directory", "/bin/nothing")

    rig = Rig(tmp_path, spawn=broken)
    response = await rig.call(RpcMethod.TERMINAL_OPEN, {"cols": 80, "rows": 24})

    assert response.code is RpcErrorCode.FAILED
    assert (response.error or "").startswith("terminal_open: the shell could not start: ")
    assert rig.terminals.count("window-1") == 0


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX PTY")
async def test_a_shell_path_that_does_not_exist_fails_without_leaking(tmp_path, monkeypatch):
    from igni_core.backend.terminal.posix_pty import PosixPty

    monkeypatch.setattr(PosixPty, "login_shell", classmethod(lambda cls: "/no/such/shell"))
    rig = Rig(tmp_path)

    response = await rig.call(RpcMethod.TERMINAL_OPEN, {"cols": 80, "rows": 24})

    assert response.code is RpcErrorCode.FAILED
    assert rig.terminals.count("window-1") == 0


async def test_an_unmounted_igni_says_it_has_no_terminal() -> None:
    wire = Wire()
    router = PoolRpcRouter(PoolReplier(wire))  # type: ignore[arg-type]
    for method in TERMINAL_RPCS:
        request = msg.RPCRequest(id=method.value, method=method.value, args={})
        request.mark_from("window-1")
        assert router.handles(request)
        await router.handle(request)
        (response,) = wire.responses(method.value)
        assert response.code is RpcErrorCode.UNAVAILABLE
        assert response.error == TerminalRpc.NOT_MOUNTED


# ── who may call ───────────────────────────────────────────────────


@pytest.mark.parametrize("method", TERMINAL_RPCS)
def test_only_igni_s_window_reaches_the_terminal(method: RpcMethod):
    assert method in standing_policy.WINDOW_ONLY_RPCS
    for standing in (Standing.PROGRAM, Standing.APPROVER):
        request = msg.RPCRequest(id="x", method=method.value)
        request.mark_from("c", standing)
        assert standing_policy.refusal(request) == f"{method.value}: only igni's window may do this"
    request = msg.RPCRequest(id="x", method=method.value)
    request.mark_from("c", Standing.WINDOW)
    assert standing_policy.refusal(request) is None


@pytest.mark.parametrize("method", TERMINAL_RPCS)
def test_the_terminal_calls_are_the_pool_s_and_no_page_or_platform_s(method: RpcMethod):
    assert method in POOL_LEVEL_RPCS
    assert method in TerminalRpc.methods
    assert TerminalRpc in POOL_RPCS
    assert method not in PAGE_RPCS
    assert method not in PLATFORM_RPCS


def test_no_extension_reaches_the_terminal():
    for method in TERMINAL_RPCS:
        assert RPC_UNREACHABLE[method] == "terminal"
    assert PUSH_UNREACHABLE[PushChannel.TERMINAL_OUTPUT] == "terminal"
    assert PUSH_UNREACHABLE[PushChannel.TERMINAL_EXITED] == "terminal"


def test_the_contract_says_what_each_call_streams_and_how_to_follow_it():
    methods = rpc_contract.build()["methods"]

    opened = methods["terminal_open"]
    assert set(opened["streams"]) == {
        "push_notification:terminal_output",
        "push_notification:terminal_exited",
    }
    assert opened["correlate"] == {"by": "payload.terminal_id", "from": "result.terminal_id"}
    assert opened["duration"] == "io"
    assert opened["scope"] == "igni"

    closed = methods["terminal_close"]
    assert closed["streams"] == ["push_notification:terminal_exited"]
    assert closed["correlate"] == {"by": "payload.terminal_id", "from": "args.terminal_id"}
    assert closed["duration"] == "io"

    for name in ("terminal_input", "terminal_resize"):
        assert methods[name]["streams"] == []
        assert methods[name]["correlate"] is None
        assert methods[name]["duration"] == "instant"


def test_the_contract_publishes_the_terminal_pushes_payloads():
    contract = rpc_contract.build()
    channels, defs = contract["push_channels"], contract["defs"]

    assert channels["terminal_output"]["schema"] == {"$ref": "#/defs/TerminalOutputPayload"}
    assert channels["terminal_exited"]["schema"] == {"$ref": "#/defs/TerminalExitedPayload"}
    assert set(defs["TerminalOutputPayload"]["properties"]) == {"terminal_id", "data"}
    assert set(defs["TerminalExitedPayload"]["properties"]) == {"terminal_id", "exit_code"}
    assert set(defs["TerminalOpened"]["properties"]) == {"terminal_id", "shell", "cwd"}
    assert set(defs["TerminalAck"]["properties"]) == {"terminal_id"}


# ── a real shell, from another connection ──────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX PTY")
async def test_another_window_s_typing_never_reaches_a_real_shell(tmp_path, monkeypatch):
    calm_bash(tmp_path / "home", monkeypatch)
    rig = Rig(tmp_path)
    try:
        terminal_id = await rig.open(client="window-1")
        await rig.type(terminal_id, 'echo "SH""ELL=$$"\r')
        await rig.wire.wait_for_output(terminal_id, "SHELL=")

        refused = await rig.call(
            RpcMethod.TERMINAL_INPUT,
            {"terminal_id": terminal_id, "data": "printf 'IN%sDER\\n' TRU\r"},
            "window-2",
        )
        closed = await rig.call(RpcMethod.TERMINAL_CLOSE, {"terminal_id": terminal_id}, "window-2")
        await rig.type(terminal_id, "printf 'OW%sER\\n' N\r")
        output = await rig.wire.wait_for_output(terminal_id, "OWNER")

        assert refused.code is closed.code is RpcErrorCode.NOT_FOUND
        assert "INTRUDER" not in output and "IN%sDER" not in output
        assert rig.wire.pushes("terminal_exited", terminal_id) == []
        assert {p.to_client for p in rig.wire.pushes("terminal_output")} == {"window-1"}
    finally:
        await rig.terminals.close_all()
    await wait_until(lambda: rig.terminals.count("window-1") == 0)

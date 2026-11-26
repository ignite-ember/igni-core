"""A window opens a real shell on a PTY, types into it, resizes it and closes it.

Driven through the pool's router and a real login bash, so what is asserted
is what a window receives: the answers, and the ``terminal_output`` and
``terminal_exited`` pushes addressed to it alone.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

import pytest

from igni_core.protocol.rpc import RpcMethod
from igni_core.protocol.schemas.rpc import RpcErrorCode
from tests.terminal_helpers import Rig, alive, calm_bash, wait_until

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="a POSIX PTY")


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    calm_bash(tmp_path / "home", monkeypatch)
    folder = tmp_path / "project"
    folder.mkdir()
    return folder


@pytest.fixture
async def rig(project: Path):
    rig = Rig(project)
    yield rig
    await rig.terminals.close_all()


def _pids(output: str, name: str) -> int:
    match = re.search(rf"{name}=(\d+)", output)
    assert match, output
    return int(match.group(1))


async def test_the_shell_starts_in_the_project_folder_and_says_which_it_is(rig, project):
    response = await rig.call(RpcMethod.TERMINAL_OPEN, {"cols": 80, "rows": 24})

    assert response.error is None
    assert set(response.result) == {"terminal_id", "shell", "cwd"}
    assert response.result["shell"] == "/bin/bash"
    assert response.result["cwd"] == str(project)
    terminal_id = response.result["terminal_id"]
    await rig.type(terminal_id, 'printf "W%sERE=%s\\n" H "$(pwd -P)"\r')
    output = await rig.wire.wait_for_output(terminal_id, "WHERE=/")
    assert f"WHERE={project.resolve()}" in output


async def test_what_is_typed_reaches_the_shell_and_what_it_prints_comes_back(rig):
    terminal_id = await rig.open()

    await rig.type(terminal_id, "printf 'MA%sER\\n' RK\r")

    output = await rig.wire.wait_for_output(terminal_id, "MARKER")
    assert "MARKER" in output


async def test_the_shell_is_a_colour_login_terminal(rig):
    terminal_id = await rig.open()

    await rig.type(
        terminal_id, 'echo "T""ERM=$TERM C""T=$COLORTERM"; shopt -q login_shell && echo "LO""GIN"\r'
    )

    output = await rig.wire.wait_for_output(terminal_id, "LOGIN")
    assert "TERM=xterm-256color CT=truecolor" in output


async def test_a_resize_changes_what_the_shell_sees(rig):
    terminal_id = await rig.open(cols=80, rows=24)
    await rig.type(terminal_id, 'echo "S""Z=$(stty size)"\r')
    await rig.wire.wait_for_output(terminal_id, "SZ=24 80")

    response = await rig.call(
        RpcMethod.TERMINAL_RESIZE, {"terminal_id": terminal_id, "cols": 132, "rows": 41}
    )
    assert response.error is None
    assert response.result == {"terminal_id": terminal_id}
    await rig.type(terminal_id, 'echo "S""Z=$(stty size)"\r')
    await rig.wire.wait_for_output(terminal_id, "SZ=41 132")


async def test_a_size_out_of_range_is_invalid(rig):
    terminal_id = await rig.open()
    for args in ({"cols": 0, "rows": 24}, {"cols": 80, "rows": 1001}):
        opened = await rig.call(RpcMethod.TERMINAL_OPEN, args)
        assert opened.code is RpcErrorCode.INVALID
        resized = await rig.call(RpcMethod.TERMINAL_RESIZE, {"terminal_id": terminal_id, **args})
        assert resized.code is RpcErrorCode.INVALID
    assert rig.terminals.count("window-1") == 1


async def test_read_waits_for_the_person_to_answer(rig):
    terminal_id = await rig.open()
    await rig.type(terminal_id, 'read LINE; echo "G""OT=$LINE"\r')
    await rig.wire.wait_for_output(terminal_id, 'echo "G""OT=$LINE"')

    await asyncio.sleep(0.3)
    assert "GOT=" not in rig.wire.output(terminal_id)

    await rig.type(terminal_id, "hello\r")
    await rig.wire.wait_for_output(terminal_id, "GOT=hello")


async def test_ctrl_c_interrupts_the_program_in_the_foreground(rig):
    terminal_id = await rig.open()
    await rig.type(terminal_id, "sleep 30\r")
    await rig.wire.wait_for_output(terminal_id, "sleep 30")

    await rig.type(terminal_id, "\x03")
    await rig.type(terminal_id, 'echo "ST""ATUS=$?"\r')

    output = await rig.wire.wait_for_output(terminal_id, "STATUS=", timeout=10)
    assert "STATUS=130" in output


async def test_closing_ends_the_shell_and_everything_it_started(rig):
    terminal_id = await rig.open()
    await rig.type(terminal_id, 'sleep 1000 & echo "B""G=$!"; echo "SH""ELL=$$"\r')
    output = await rig.wire.wait_for_output(terminal_id, "SHELL=")
    background, shell = _pids(output, "BG"), _pids(output, "SHELL")
    await rig.type(terminal_id, 'sh -c \'echo "F""G=$$"; exec sleep 1001\'\r')
    foreground = _pids(await rig.wire.wait_for_output(terminal_id, "FG="), "FG")
    assert alive(shell) and alive(background) and alive(foreground)

    response = await rig.call(RpcMethod.TERMINAL_CLOSE, {"terminal_id": terminal_id})

    assert response.error is None
    assert response.result == {"terminal_id": terminal_id}
    # Its end was pushed before the answer.
    exited = rig.wire.pushes("terminal_exited", terminal_id)
    assert len(exited) == 1
    assert rig.wire.sent.index(exited[0]) < rig.wire.sent.index(response)
    await wait_until(lambda: not any(alive(p) for p in (shell, background, foreground)), 10)
    with pytest.raises(ProcessLookupError):
        os.killpg(shell, 0)
    assert rig.terminals.count("window-1") == 0


async def test_a_shell_that_exits_is_told_once_with_its_code(rig):
    terminal_id = await rig.open()

    await rig.type(terminal_id, "exit 7\r")

    await wait_until(lambda: bool(rig.wire.pushes("terminal_exited", terminal_id)))
    await wait_until(lambda: rig.terminals.count("window-1") == 0)
    (exited,) = rig.wire.pushes("terminal_exited", terminal_id)
    assert exited.payload == {"terminal_id": terminal_id, "exit_code": 7}
    # Nothing after the end, and the ended terminal is gone.
    assert rig.wire.sent[-1] is exited
    typed = await rig.call(RpcMethod.TERMINAL_INPUT, {"terminal_id": terminal_id, "data": "x"})
    assert typed.code is RpcErrorCode.NOT_FOUND
    closed = await rig.call(RpcMethod.TERMINAL_CLOSE, {"terminal_id": terminal_id})
    assert closed.code is RpcErrorCode.NOT_FOUND
    assert len(rig.wire.pushes("terminal_exited", terminal_id)) == 1


async def test_every_push_is_addressed_to_the_window_that_opened_it(rig):
    terminal_id = await rig.open(client="window-7")
    await rig.type(terminal_id, "printf 'MA%sER\\n' RK; exit\r", client="window-7")
    await wait_until(lambda: bool(rig.wire.pushes("terminal_exited", terminal_id)))

    pushes = rig.wire.pushes("terminal_output") + rig.wire.pushes("terminal_exited")
    assert pushes
    assert {p.to_client for p in pushes} == {"window-7"}


async def test_a_window_that_goes_away_takes_its_terminals_with_it(rig):
    first = await rig.open(client="window-1")
    second = await rig.open(client="window-1")
    other = await rig.open(client="window-2")
    pids = {}
    for terminal_id, client in ((first, "window-1"), (second, "window-1"), (other, "window-2")):
        await rig.type(terminal_id, 'echo "SH""ELL=$$"\r', client=client)
        pids[terminal_id] = _pids(await rig.wire.wait_for_output(terminal_id, "SHELL="), "SHELL")

    rig.terminals.connection_watcher("detached", "window-1")

    await wait_until(lambda: rig.terminals.count("window-1") == 0)
    await wait_until(lambda: not alive(pids[first]) and not alive(pids[second]))
    assert rig.terminals.count("window-2") == 1
    assert alive(pids[other])
    assert rig.wire.pushes("terminal_exited", other) == []


async def test_an_attaching_window_ends_nothing(rig):
    terminal_id = await rig.open()

    rig.terminals.connection_watcher("attached", "window-1")

    assert rig.terminals.count("window-1") == 1
    await rig.type(terminal_id, "printf 'ST%sL\\n' IL\r")
    await rig.wire.wait_for_output(terminal_id, "STILL")


async def test_closing_every_terminal_when_igni_stops(rig):
    first = await rig.open(client="window-1")
    second = await rig.open(client="window-2")

    await rig.terminals.close_all()

    assert rig.terminals.count("window-1") == rig.terminals.count("window-2") == 0
    assert len(rig.wire.pushes("terminal_exited", first)) == 1
    assert len(rig.wire.pushes("terminal_exited", second)) == 1

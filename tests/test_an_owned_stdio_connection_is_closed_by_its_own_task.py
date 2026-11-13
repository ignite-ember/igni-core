"""One stdio connection lives in a task of its own and is closed from any task.

:class:`OwnedStdioConnection` with a real stdio server process; the session
is a small context manager so the close path can be made to stall.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import traceback
from typing import Any

import pytest
from mcp.client.stdio import StdioServerParameters

from igni_core.core.mcp.stdio_binding import OwnedStdioConnection

pytestmark = pytest.mark.asyncio

SLEEPER = "import time\ntime.sleep(60)\n"


class _Session:
    def __init__(self, *, hang_on_exit: bool = False, fail: BaseException | None = None) -> None:
        self.exited = False
        self._hang = hang_on_exit
        self._fail = fail

    async def __aenter__(self) -> _Session:
        if self._fail is not None:
            raise self._fail
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._hang:
            await asyncio.sleep(60)
        self.exited = True


class _Fatal(BaseException):
    pass


def _connection(tmp_path: Any, session: _Session, script: str = SLEEPER):
    path = tmp_path / "server.py"
    path.write_text(script)
    errlog = open(tmp_path / "log", "a")  # noqa: SIM115
    params = StdioServerParameters(command=sys.executable, args=[str(path)])
    connection = OwnedStdioConnection(
        params, errlog=errlog, session_for=lambda read, write: session, name="owned"
    )
    return connection, errlog


async def test_opening_hands_back_the_session_and_closing_exits_it_and_the_log(tmp_path):
    session = _Session()
    connection, errlog = _connection(tmp_path, session)

    assert await connection.open() is session
    assert session.exited is False
    await connection.aclose()

    assert session.exited is True
    assert errlog.closed


async def test_any_task_may_close_it_and_closing_twice_or_unopened_is_harmless(tmp_path):
    session = _Session()
    connection, _ = _connection(tmp_path, session)
    await OwnedStdioConnection.aclose(connection)  # before open: nothing to close

    await connection.open()
    await asyncio.create_task(connection.aclose())
    await connection.aclose()
    assert session.exited is True


async def test_leaving_it_as_a_context_manager_closes_it(tmp_path):
    session = _Session()
    connection, _ = _connection(tmp_path, session)
    await connection.open()
    await connection.__aexit__(None, None, None)
    assert session.exited is True


async def test_a_close_that_stalls_is_cancelled_after_the_timeout_and_never_raises(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(OwnedStdioConnection, "CLOSE_TIMEOUT", 0.2)
    session = _Session(hang_on_exit=True)
    connection, errlog = _connection(tmp_path, session)
    await connection.open()

    await asyncio.wait_for(connection.aclose(), 5)
    task = connection._task
    assert task is not None
    await asyncio.wait({task}, timeout=20)

    assert task.done()
    assert session.exited is False
    assert errlog.closed


async def test_an_error_while_opening_reaches_the_opener_and_closes_the_log(tmp_path):
    connection, errlog = _connection(tmp_path, _Session(fail=RuntimeError("no handshake")))
    # The transport's own task group wraps what its session raises.
    with pytest.raises(Exception) as raised:  # noqa: B017 — an ExceptionGroup
        await connection.open()
    assert "no handshake" in "".join(traceback.format_exception(raised.value))
    assert errlog.closed


async def test_a_failure_that_is_not_an_exception_reaches_the_opener_as_a_connection_error(
    tmp_path,
):
    class Params:
        @property
        def command(self) -> str:
            raise _Fatal("fatal")

    errlog = open(tmp_path / "log", "a")  # noqa: SIM115
    connection = OwnedStdioConnection(
        Params(), errlog=errlog, session_for=lambda r, w: _Session(), name="owned"
    )
    with pytest.raises(ConnectionError, match="fatal"):
        await connection.open()
    assert errlog.closed


async def test_a_server_that_ends_by_itself_is_not_an_error_for_a_later_close(tmp_path):
    connection, _ = _connection(tmp_path, _Session(), script="pass\n")
    await connection.open()
    await asyncio.sleep(0.3)
    with contextlib.suppress(Exception):
        await connection.aclose()

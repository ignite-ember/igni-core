"""One attached view's wrapper: how a send ended, and a close that never raises.

The transport evicts on a failed result and logs at the level the reason
asks for, so the reason must be right: a wedged client is a timeout, a
client whose connection broke is a connection error.
"""

from __future__ import annotations

import asyncio

import pytest

from igni_core.transport._ws_client import MirroredClient, SendResult


class _Conn:
    def __init__(self, *, send_error: BaseException | None = None, delay: float = 0.0) -> None:
        self.frames: list[str] = []
        self.closed = 0
        self.close_error: BaseException | None = None
        self._send_error = send_error
        self._delay = delay

    async def send(self, payload: str) -> None:
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._send_error is not None:
            raise self._send_error
        self.frames.append(payload)

    async def close(self) -> None:
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


async def test_a_delivered_frame_is_ok_and_names_its_client():
    conn = _Conn()
    client = MirroredClient("ws-1", conn)  # type: ignore[arg-type]
    result = await client.send("frame", timeout=1.0)
    assert result == SendResult(client_id="ws-1", ok=True, reason=None)
    assert client.id == "ws-1"
    assert conn.frames == ["frame"]


async def test_a_wedged_client_is_a_timeout():
    client = MirroredClient("ws-2", _Conn(delay=5.0))  # type: ignore[arg-type]
    result = await client.send("frame", timeout=0.05)
    assert result == SendResult(client_id="ws-2", ok=False, reason="timeout")


async def test_a_broken_connection_is_a_connection_error_not_a_timeout():
    for error in (ConnectionResetError("gone"), RuntimeError("closed"), OSError("pipe")):
        client = MirroredClient("ws-3", _Conn(send_error=error))  # type: ignore[arg-type]
        result = await client.send("frame", timeout=1.0)
        assert result == SendResult(client_id="ws-3", ok=False, reason="conn_error"), error


async def test_a_send_that_raises_does_not_log(caplog):
    caplog.set_level("DEBUG")
    client = MirroredClient("ws-4", _Conn(send_error=RuntimeError("x")))  # type: ignore[arg-type]
    await client.send("frame", timeout=1.0)
    assert caplog.records == []


async def test_closing_closes_the_connection_and_swallows_what_it_raises():
    conn = _Conn()
    await MirroredClient("ws-5", conn).close()  # type: ignore[arg-type]
    assert conn.closed == 1

    conn = _Conn()
    conn.close_error = ConnectionResetError("already gone")
    await MirroredClient("ws-6", conn).close()  # type: ignore[arg-type]
    assert conn.closed == 1


async def test_a_close_that_is_cancelled_is_not_swallowed():
    conn = _Conn()
    conn.close_error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await MirroredClient("ws-7", conn).close()  # type: ignore[arg-type]

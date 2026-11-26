"""What a terminal prints arrives whole, in order, gathered and bounded.

A UTF-8 character split across two reads arrives whole; bytes that are not
UTF-8 become U+FFFD. Reads that come close together go out as one push, and
no push holds more than 64 KiB of text. While the window falls behind the
PTY is not read. ``terminal_exited`` comes once, after the last output.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from igni_core.backend.terminal.output_buffer import TerminalOutputBuffer
from igni_core.backend.terminal.terminal import Terminal
from igni_core.protocol import messages as msg
from tests.terminal_helpers import ScriptedPty, Wire, wait_until

# ── the buffer ─────────────────────────────────────────────────────


def test_a_character_split_across_reads_arrives_whole():
    buffer = TerminalOutputBuffer()
    snowman = "☃".encode()  # three bytes
    buffer.feed(b"a" + snowman[:1])
    assert buffer.drain() == ["a"]
    buffer.feed(snowman[1:2])
    assert buffer.drain() == []
    buffer.feed(snowman[2:] + b"b")
    assert buffer.drain() == ["☃b"]


def test_a_four_byte_character_fed_byte_by_byte_arrives_whole():
    buffer = TerminalOutputBuffer()
    for byte in "🐈".encode():
        buffer.feed(bytes([byte]))
    assert buffer.drain() == ["🐈"]


def test_bytes_that_are_not_utf8_become_replacement_characters():
    buffer = TerminalOutputBuffer()
    buffer.feed(b"ok\xff\xfeend")
    assert buffer.drain() == ["ok��end"]


def test_a_partial_character_left_at_the_end_becomes_one_replacement():
    buffer = TerminalOutputBuffer()
    buffer.feed(b"x" + "é".encode()[:1])
    buffer.finish()
    assert buffer.drain() == ["x�"]
    assert buffer.size == 0


def test_text_is_handed_out_in_chunks_no_larger_than_the_cap():
    buffer = TerminalOutputBuffer(max_chunk=4)
    buffer.feed(b"abcdefghij")
    assert buffer.size == 10
    assert buffer.drain() == ["abcd", "efgh", "ij"]
    assert buffer.size == 0
    assert buffer.drain() == []


def test_the_default_cap_is_64_kib():
    buffer = TerminalOutputBuffer()
    buffer.feed(b"x" * (64 * 1024 + 1))
    assert [len(c) for c in buffer.drain()] == [64 * 1024, 1]


def test_a_cap_below_one_is_refused():
    with pytest.raises(ValueError, match="max_chunk"):
        TerminalOutputBuffer(max_chunk=0)


# ── the pushes ─────────────────────────────────────────────────────


class _Rig:
    def __init__(self) -> None:
        self.wire = Wire()
        self.pty = ScriptedPty(Path("/p"), 80, 24)
        self.ended: list[tuple[str, int | None]] = []
        self.terminal = Terminal(
            terminal_id="term-1",
            client_id="window-1",
            process=self.pty,
            cwd="/p",
            send=self.wire.send,
        )

    def start(self) -> None:
        self.terminal.start(lambda t, code: self.ended.append((t.terminal_id, code)))

    def outputs(self) -> list[str]:
        return [str(p.payload["data"]) for p in self.wire.pushes("terminal_output")]


async def test_reads_close_together_go_out_as_one_push():
    rig = _Rig()
    rig.start()
    for part in (b"he", b"ll", b"o"):
        rig.pty.prints(part)

    await wait_until(lambda: bool(rig.outputs()))
    await asyncio.sleep(Terminal.COALESCE_S * 3)

    assert rig.outputs() == ["hello"]
    (push,) = rig.wire.pushes("terminal_output")
    assert push.payload == {"terminal_id": "term-1", "data": "hello"}
    assert push.to_client == "window-1"


async def test_reads_far_apart_go_out_as_separate_pushes_in_order():
    rig = _Rig()
    rig.start()
    rig.pty.prints(b"one ")
    await wait_until(lambda: rig.outputs() == ["one "])
    rig.pty.prints(b"two")
    await wait_until(lambda: len(rig.outputs()) == 2)
    assert rig.outputs() == ["one ", "two"]


async def test_a_burst_larger_than_the_cap_is_split_across_pushes():
    rig = _Rig()
    rig.start()
    rig.pty.prints(b"y" * (TerminalOutputBuffer.MAX_CHUNK * 2 + 10))

    await wait_until(lambda: len(rig.outputs()) == 3)
    assert [len(o) for o in rig.outputs()] == [
        TerminalOutputBuffer.MAX_CHUNK,
        TerminalOutputBuffer.MAX_CHUNK,
        10,
    ]


async def test_the_exit_comes_once_after_the_last_output():
    rig = _Rig()
    rig.start()
    rig.pty.prints(b"bye" + "é".encode()[:1])
    rig.pty.exit(3)

    await wait_until(lambda: bool(rig.ended))
    kinds = [m.channel for m in rig.wire.sent if isinstance(m, msg.PushNotification)]
    assert kinds == ["terminal_output", "terminal_exited"]
    assert rig.outputs() == ["bye�"]
    (exited,) = rig.wire.pushes("terminal_exited")
    assert exited.payload == {"terminal_id": "term-1", "exit_code": 3}
    assert exited.to_client == "window-1"
    assert rig.ended == [("term-1", 3)]
    rig.pty.exit(4)
    await asyncio.sleep(0.05)
    assert len(rig.wire.pushes("terminal_exited")) == 1
    assert rig.ended == [("term-1", 3)]


async def test_an_unknown_exit_code_is_null():
    rig = _Rig()
    rig.start()
    rig.pty.exit(None)
    await wait_until(lambda: bool(rig.ended))
    assert rig.wire.pushes("terminal_exited")[0].payload["exit_code"] is None


async def test_closing_waits_for_the_exit_push_and_returns_the_code():
    rig = _Rig()
    rig.start()

    code = await rig.terminal.close()

    assert code == -1
    assert rig.pty.closed == 1
    assert len(rig.wire.pushes("terminal_exited")) == 1


async def test_a_shell_that_never_exits_still_lets_close_return(monkeypatch):
    rig = _Rig()
    monkeypatch.setattr(Terminal, "FLUSH_TIMEOUT_S", 0.05)

    async def stuck() -> None:
        rig.pty.closed += 1

    monkeypatch.setattr(rig.pty, "close", stuck)
    rig.start()

    assert await rig.terminal.close() is None
    # The pump was stopped, and the end was still recorded once.
    assert rig.ended == [("term-1", None)]


async def test_a_window_that_falls_behind_stops_the_reading_until_it_catches_up(monkeypatch):
    rig = _Rig()
    monkeypatch.setattr(Terminal, "HIGH_WATER", 8)
    gate = asyncio.Event()
    sent: list[msg.Message] = []

    async def slow_send(message: msg.Message) -> None:
        await gate.wait()
        sent.append(message)

    rig.terminal._send_message = slow_send
    rig.start()

    rig.pty.prints(b"1234")
    assert not rig.pty.paused
    rig.pty.prints(b"5678")
    assert rig.pty.paused and rig.pty.pauses == 1
    rig.pty.prints(b"9")
    assert rig.pty.pauses == 1

    gate.set()
    await wait_until(lambda: not rig.pty.paused)
    assert rig.pty.resumes == 1
    assert "".join(str(m.payload["data"]) for m in sent) == "123456789"


async def test_a_push_that_fails_to_go_out_does_not_stop_the_terminal():
    rig = _Rig()
    failures = 0

    async def flaky(message: msg.Message) -> None:
        nonlocal failures
        if failures == 0:
            failures += 1
            raise ConnectionError("gone")
        await rig.wire.send(message)

    rig.terminal._send_message = flaky
    rig.start()
    rig.pty.prints(b"lost")
    await wait_until(lambda: failures == 1)
    await asyncio.sleep(Terminal.COALESCE_S * 3)
    rig.pty.prints(b"kept")
    await wait_until(lambda: rig.outputs() == ["kept"])


async def test_typed_text_reaches_the_pty_as_utf8():
    rig = _Rig()
    rig.start()
    rig.terminal.write("ü\x03")
    rig.terminal.resize(10, 5)
    assert rig.pty.typed == "ü\x03".encode()
    assert rig.pty.size == (10, 5)
    assert rig.terminal.shell == "/bin/scripted"
    await rig.terminal.close()

"""One open terminal: its shell, and the pushes that carry what it prints."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable

from igni_core.backend.terminal.output_buffer import TerminalOutputBuffer
from igni_core.backend.terminal.terminal_process import TerminalProcess
from igni_core.core.activity.background import BackgroundTasks
from igni_core.protocol import messages as msg
from igni_core.protocol.schemas.push import push_terminal_exited, push_terminal_output
from igni_core.protocol.schemas.push_payloads import TerminalExitedPayload, TerminalOutputPayload

logger = logging.getLogger(__name__)

#: Sends one message to igni's windows.
Send = Callable[[msg.Message], Awaitable[None]]


class Terminal:
    """A shell for one connection; what it prints goes to that connection alone.

    Output is gathered for :attr:`COALESCE_S` before it is sent, so a burst
    of small reads goes out as one push; a push holds at most
    :attr:`TerminalOutputBuffer.MAX_CHUNK` characters. While more than
    :attr:`HIGH_WATER` characters wait to be sent, the PTY is not read, so
    a shell that prints faster than the window takes it waits.
    """

    COALESCE_S = 0.015
    HIGH_WATER = 1024 * 1024
    #: How long :meth:`close` waits for the last pushes once the shell has gone.
    FLUSH_TIMEOUT_S = 2.0

    def __init__(
        self,
        *,
        terminal_id: str,
        client_id: str,
        process: TerminalProcess,
        cwd: str,
        send: Send,
    ) -> None:
        self.terminal_id = terminal_id
        self.client_id = client_id
        self.cwd = cwd
        self._process = process
        self._send_message = send
        self._buffer = TerminalOutputBuffer()
        self._wake = asyncio.Event()
        self._paused = False
        self._exited = False
        self._exit_code: int | None = None
        self._pump: asyncio.Task[None] | None = None
        self._ended: asyncio.Future[int | None] | None = None
        self._on_ended: Callable[[Terminal, int | None], None] | None = None
        self.opened_at = time.monotonic()

    @property
    def shell(self) -> str:
        return self._process.shell

    def start(self, on_ended: Callable[[Terminal, int | None], None]) -> None:
        """Start the shell; ``on_ended`` is called once, after ``terminal_exited`` went out."""
        loop = asyncio.get_running_loop()
        self._process.start(loop, self._output, self._exit)
        self._on_ended = on_ended
        self._ended = loop.create_future()
        # Not busy: an open terminal does not keep igni awake.
        self._pump = BackgroundTasks.shared().daemon(
            self._run_pump(), name=f"terminal-{self.terminal_id}", loop=loop
        )

    def write(self, data: str) -> None:
        """Type ``data`` into the shell."""
        self._process.write(data.encode("utf-8", errors="replace"))

    def resize(self, cols: int, rows: int) -> None:
        self._process.resize(cols, rows)

    async def close(self) -> int | None:
        """End the shell and wait for its ``terminal_exited``; returns its exit code."""
        await self._process.close()
        ended = self._ended
        if ended is None:
            return None
        try:
            return await asyncio.wait_for(asyncio.shield(ended), self.FLUSH_TIMEOUT_S)
        except TimeoutError:
            if self._pump is not None:
                self._pump.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._pump
            return self._exit_code

    def _output(self, data: bytes) -> None:
        self._buffer.feed(data)
        if not self._paused and self._buffer.size >= self.HIGH_WATER:
            self._paused = True
            self._process.pause_reading()
        self._wake.set()

    def _exit(self, code: int | None) -> None:
        self._buffer.finish()
        self._exit_code = code
        self._exited = True
        self._wake.set()

    async def _run_pump(self) -> None:
        try:
            while True:
                await self._wake.wait()
                if not self._exited:
                    await asyncio.sleep(self.COALESCE_S)
                self._wake.clear()
                for chunk in self._buffer.drain():
                    payload = TerminalOutputPayload(terminal_id=self.terminal_id, data=chunk)
                    await self._send(push_terminal_output(payload))
                if self._paused:
                    self._paused = False
                    self._process.resume_reading()
                if self._exited and not self._buffer.size:
                    payload_exit = TerminalExitedPayload(
                        terminal_id=self.terminal_id, exit_code=self._exit_code
                    )
                    await self._send(push_terminal_exited(payload_exit))
                    return
        finally:
            if self._ended is not None and not self._ended.done():
                self._ended.set_result(self._exit_code)
            if self._on_ended is not None:
                on_ended, self._on_ended = self._on_ended, None
                on_ended(self, self._exit_code)

    async def _send(self, push: msg.PushNotification) -> None:
        """To the connection that opened the terminal, never to every window."""
        try:
            await self._send_message(push.address_to(self.client_id))
        except Exception:
            logger.debug("terminal %s: a push did not go out", self.terminal_id, exc_info=True)


__all__ = ["Terminal"]

"""Run the decisions service: ``python -m igni_core.decisions_server``.

Two ways it runs:

* **Beside a desktop backend.** The backend starts it on first use
  (:class:`~igni_core.core.decisions.endpoints.LocalDecisions`) on a
  loopback port, with ``--ready-file`` (where the bound port is written),
  ``--watch-stdin`` (it exits when the backend's end of its stdin
  closes) and ``--parent-pid`` (it exits when the backend is gone), so
  it never outlives the backend, and ``--idle-seconds`` so an unused
  model gives its memory back.
* **Once per node in the cloud.** ``--host 0.0.0.0 --port 8730`` with no
  idle limit; every backend on the node points ``IGNI_DECISIONS_URL``
  at it.

The secret callers must send is ``IGNI_DECISIONS_SECRET``; it refuses
to start without one. Told to stop, in-flight calls get
:attr:`DecisionsServer.GRACE_S` and the process ends: a model still
loading never holds it open.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
import time
from pathlib import Path

import click
import uvicorn

from igni_core.backend.supervisor import ParentProcessProbe
from igni_core.core.activity import background
from igni_core.decisions_server.app import DecisionsApp
from igni_core.decisions_server.model import LayaModel

SECRET_ENV = "IGNI_DECISIONS_SECRET"  # noqa: S105 — an environment variable's name, not a secret


class DecisionsServer:
    """One uvicorn server over :class:`DecisionsApp`, with the ways it stops itself."""

    #: How often the idle limit is checked.
    IDLE_POLL_S = 5.0
    #: How long in-flight calls get once the service is told to stop.
    GRACE_S = 3.0
    #: How often the parent is checked.
    PARENT_POLL_S = 1.0

    def __init__(self, app: DecisionsApp, *, idle_seconds: float) -> None:
        self._app = app
        self._idle_seconds = idle_seconds
        self._server = uvicorn.Server(
            uvicorn.Config(
                app.asgi,
                log_level="warning",
                access_log=False,
                lifespan="off",
                timeout_graceful_shutdown=self.GRACE_S,
            )
        )

    def serve(self, sock: socket.socket) -> None:
        """Serve until stopped, then end the process without waiting on a model call."""
        asyncio.run(self._serve(sock, exit_after=True))

    def stop(self) -> None:
        self._server.should_exit = True

    def watch_stdin(self) -> None:
        """Stop when whoever started us goes away (their end of our stdin closes)."""

        def drain() -> None:
            while sys.stdin.buffer.read(4096):
                pass
            self.stop()

        background.thread(drain, name="decisions-stdin")

    def watch_parent(self, pid: int, probe: ParentProcessProbe | None = None) -> None:
        """Stop when the process that started us has gone, whoever holds our stdin."""
        alive = (probe or ParentProcessProbe()).is_alive
        reparents = sys.platform != "win32"

        def watch() -> None:
            while alive(pid) and not (reparents and os.getppid() != pid):
                time.sleep(self.PARENT_POLL_S)
            self.stop()

        background.thread(watch, name="decisions-parent")

    async def _serve(self, sock: socket.socket, *, exit_after: bool = False) -> None:
        idle = (
            background.daemon(self._idle_out(), name="decisions-idle")
            if self._idle_seconds > 0
            else None
        )
        try:
            await self._server.serve(sockets=[sock])
        finally:
            if idle is not None:
                idle.cancel()
            if exit_after:
                self._exit()

    @staticmethod
    def _exit() -> None:
        """End now: asyncio would otherwise wait on a model thread that may never finish."""
        logging.shutdown()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)

    async def _idle_out(self) -> None:
        while not self._server.should_exit:
            await asyncio.sleep(self.IDLE_POLL_S)
            if self._app.activity.idle_for() >= self._idle_seconds:
                self.stop()


class ReadyFile:
    """Where the bound port is written, whole, once the socket is listening."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def write(self, port: int) -> None:
        partial = self._path.with_suffix(".partial")
        partial.write_text(str(port))
        os.replace(partial, self._path)


@click.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=0, show_default=True, type=int, help="0 picks a free port.")
@click.option("--ready-file", type=click.Path(path_type=Path), default=None)
@click.option("--idle-seconds", default=0.0, show_default=True, help="0 never idles out.")
@click.option("--watch-stdin", is_flag=True, help="Exit when stdin closes.")
@click.option("--parent-pid", default=0, type=int, help="Exit when this process is gone.")
def main(
    host: str,
    port: int,
    ready_file: Path | None,
    idle_seconds: float,
    watch_stdin: bool,
    parent_pid: int,
) -> None:
    secret = os.environ.get(SECRET_ENV, "")
    if not secret:
        raise click.UsageError(f"{SECRET_ENV} must be set")
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    app = DecisionsApp(LayaModel(), secret)
    server = DecisionsServer(app, idle_seconds=idle_seconds)
    sock = socket.create_server((host, port))
    if ready_file is not None:
        ReadyFile(ready_file).write(sock.getsockname()[1])
    if watch_stdin:
        server.watch_stdin()
    if parent_pid > 0:
        server.watch_parent(parent_pid)
    server.serve(sock)


if __name__ == "__main__":
    main()

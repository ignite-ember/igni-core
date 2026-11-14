"""Where igni's decisions service is, and how to reach it.

* ``IGNI_DECISIONS_URL`` set (the cloud: one service per node) —
  :class:`RemoteDecisions`, sending ``IGNI_DECISIONS_TOKEN``;
* otherwise (a desktop) — :class:`LocalDecisions`, which starts the
  service on first use as its own process on a loopback port, with a
  per-run secret, and starts it again if it has idled out.

A service that will not start is
:class:`~.schemas.DecisionsUnavailable`.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict

from igni_core.core.decisions.schemas import DecisionsUnavailable

URL_ENV = "IGNI_DECISIONS_URL"
TOKEN_ENV = "IGNI_DECISIONS_TOKEN"  # noqa: S105 — an environment variable's name, not a secret
SECRET_ENV = "IGNI_DECISIONS_SECRET"  # noqa: S105 — an environment variable's name, not a secret


class Connection(BaseModel):
    """Where to send a call, and with what."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    base_url: str
    token: str
    transport: httpx.AsyncBaseTransport | None = None


class DecisionsEndpoint(Protocol):
    """Where the decisions service is. Blocking; called off the event loop."""

    def connection(self) -> Connection: ...

    def lost(self) -> None:
        """The last call could not connect; find the service again next time."""


class RemoteDecisions:
    """A service someone else runs — one per cloud node."""

    def __init__(
        self, url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._connection = Connection(base_url=url.rstrip("/"), token=token, transport=transport)

    def connection(self) -> Connection:
        return self._connection

    def lost(self) -> None:
        return None


class LocalDecisions:
    """The service as this backend's own child process, started on first use."""

    #: How long the service may take to start listening (not to load the model).
    START_S = 30.0
    #: An unused service exits after this long and gives its memory back.
    IDLE_S = 900

    def __init__(self) -> None:
        self._secret = secrets.token_urlsafe(32)
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._home: Path | None = None
        self._url = ""

    def connection(self) -> Connection:
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                self._url = self._start()
            return Connection(base_url=self._url, token=self._secret)

    def lost(self) -> None:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                self._process.terminate()
            self._process = None

    def _start(self) -> str:
        if self._home is not None:
            # The last service has gone; its port file and log go with it.
            shutil.rmtree(self._home, ignore_errors=True)
        home = self._home = Path(tempfile.mkdtemp(prefix="igni-decisions-"))
        ready = home / "port"
        log = (home / "service.log").open("ab")
        env = dict(os.environ, **{SECRET_ENV: self._secret})
        command = [
            sys.executable,
            "-m",
            "igni_core.decisions_server",
            "--port",
            "0",
            "--ready-file",
            str(ready),
            "--idle-seconds",
            str(self.IDLE_S),
            "--watch-stdin",
        ]
        try:
            process = subprocess.Popen(  # noqa: S603 — our own module, by absolute interpreter
                command, stdin=subprocess.PIPE, stdout=log, stderr=log, env=env
            )
        except OSError as exc:
            raise DecisionsUnavailable(f"igni's decisions service would not start ({exc})") from exc
        finally:
            log.close()
        deadline = time.monotonic() + self.START_S
        while not ready.exists():
            if process.poll() is not None:
                raise DecisionsUnavailable(
                    "igni's decisions service exited as it started; see "
                    f"{home / 'service.log'} (is igni-core[decisions] installed?)"
                )
            if time.monotonic() > deadline:
                process.kill()
                process.wait()
                raise DecisionsUnavailable("igni's decisions service did not start in time")
            time.sleep(0.05)
        self._process = process
        return f"http://127.0.0.1:{int(ready.read_text())}"


class DecisionsEndpoints:
    """Which endpoint this process uses."""

    @staticmethod
    def configured() -> DecisionsEndpoint:
        url = os.environ.get(URL_ENV, "").strip()
        if url:
            return RemoteDecisions(url, os.environ.get(TOKEN_ENV, ""))
        return LocalDecisions()


__all__ = [
    "Connection",
    "DecisionsEndpoint",
    "DecisionsEndpoints",
    "LocalDecisions",
    "RemoteDecisions",
]

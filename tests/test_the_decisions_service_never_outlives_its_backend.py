"""The decisions service never outlives the backend that started it, however the backend ends.

A full run of igni-app's live-backend end-to-end suite left nineteen
``igni_core.decisions_server`` processes running after their backends
had gone: each was still loading Laya (an ~800 MB download into its
test's home), and a service told to stop waited for that load to end,
so the home could not be removed either. Here the real backend starts
the real service, whose Laya never finishes loading (a stand-in
``laya`` on its path), and the backend is ended every way it can end:
its window quitting, SIGTERM, SIGINT, and being killed outright.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from websockets.asyncio.client import connect

from igni_core.backend.supervisor import ParentProcessProbe
from igni_core.core.decisions.endpoints import LocalDecisions

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")

#: How long a service may take to go once its backend has: its grace (3 s), plus a margin.
GONE_S = 10.0

LAYA_THAT_NEVER_LOADS = '''
import os
import time
from pathlib import Path


class Router:
    """Laya's router, as a download that never ends."""

    def __init__(self, **kwargs):
        Path(os.environ["LAYA_LOADING_FILE"]).write_text(str(os.getpid()))
        while True:
            time.sleep(3600)
'''

QUESTION = {
    "state": {"message": "thanks"},
    "questions": [{"id": "q", "kind": "noul", "question": "Is `message` thanks?"}],
    "model": "typed-decisions",
}


class Rig:
    """A temp home, a project, a stand-in Laya, and the environment that names them."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.project = root / "project"
        self.tmp = root / "tmp"
        self.loading = root / "laya-loading"
        stub = root / "stub"
        for path in (self.home, self.project / ".igni", self.tmp, stub / "laya"):
            path.mkdir(parents=True)
        (stub / "laya" / "__init__.py").write_text(LAYA_THAT_NEVER_LOADS)
        self.token = secrets.token_urlsafe(24)
        self.env = {
            k: v for k, v in os.environ.items() if not k.startswith("IGNI_") and k != "PYTHONPATH"
        }
        self.env.update(
            HOME=str(self.home),
            USERPROFILE=str(self.home),
            TMPDIR=str(self.tmp),
            TEMP=str(self.tmp),
            TMP=str(self.tmp),
            HF_HOME=str(root / "hf"),
            PYTHONPATH=str(stub),
            PYTHONUNBUFFERED="1",
            AGNO_TELEMETRY="false",
            IGNI_WS_TOKEN=self.token,
            IGNI_DECISIONS_SECRET="s3cret",
            LAYA_LOADING_FILE=str(self.loading),
        )
        self.spawned: list[int] = []

    def configure(self) -> None:
        config = self.home / ".igni" / "config.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            "update_check_ttl: 0\n"
            "models:\n"
            "  default: nowhere\n"
            "  registry:\n"
            "    nowhere:\n"
            "      provider: openai_like\n"
            "      model_id: nowhere\n"
            "      url: http://127.0.0.1:9/v1\n"
            "      api_key: unused\n"
            "      context_window: 200000\n"
        )

    def service_pid(self, timeout: float = 60.0) -> int:
        """The service's pid, once its Laya has started loading."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.loading.exists() and self.loading.read_text():
                pid = int(self.loading.read_text())
                self.spawned.append(pid)
                return pid
            time.sleep(0.1)
        raise AssertionError("the service never started loading Laya")

    def service_homes(self) -> list[Path]:
        return sorted(self.tmp.glob("igni-decisions-*"))


def running(pid: int) -> bool:
    """Alive and not a zombie waiting to be reaped."""
    if not ParentProcessProbe().is_alive(pid):
        return False
    if sys.platform == "win32":
        return True
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
    ).stdout.strip()
    return bool(state) and not state.startswith("Z")


def gone_within(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not running(pid):
            return True
        time.sleep(0.1)
    return False


def force_kill(pid: int) -> None:
    if running(pid):
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL if sys.platform != "win32" else signal.SIGTERM)


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    made = Rig(tmp_path)
    made.configure()
    yield made
    for pid in made.spawned:
        force_kill(pid)


# ── The real backend ─────────────────────────────────────────────


class Backend:
    """``python -m igni_core.backend --ws-port 0``, as the desktop app starts it."""

    def __init__(self, rig: Rig, *, parent_pid: int = 0) -> None:
        env = dict(rig.env)
        if parent_pid:
            env["IGNI_PARENT_PID"] = str(parent_pid)
        self._rig = rig
        self._log = (rig.root / "backend.log").open("ab")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "igni_core.backend", "--ws-port", "0", "--project-dir", "."],
            cwd=rig.project,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=self._log,
            start_new_session=sys.platform != "win32",
        )
        rig.spawned.append(self.process.pid)
        self.port = self._ready_port()

    def _ready_port(self) -> int:
        assert self.process.stdout is not None
        for raw in self.process.stdout:
            line = raw.decode(errors="replace").strip()
            if line.startswith("{") and json.loads(line).get("status") == "ready":
                return int(json.loads(line)["ws_port"])
        raise AssertionError(f"the backend never said it was ready:\n{self.log_tail()}")

    def log_tail(self) -> str:
        return "\n".join((self._rig.root / "backend.log").read_text().splitlines()[-40:])

    async def say(self, text: str) -> None:
        """Send one chat message, which the backend asks Laya how it feels about."""
        async with connect(
            f"ws://127.0.0.1:{self.port}",
            subprotocols=["igni.v1", f"igni-token.{self._rig.token}"],  # type: ignore[list-item]
            max_size=None,
            open_timeout=30,
        ) as ws:
            await self._until(ws, "welcome")
            await ws.send(
                json.dumps(
                    {"type": "rpc_request", "id": "attach", "method": "attach_session", "args": {}}
                )
            )
            await self._until(ws, "rpc_response")
            await ws.send(
                json.dumps(
                    {"type": "user_message", "id": "run-1", "text": text, "file_contents": {}}
                )
            )
            await asyncio.to_thread(self._rig.service_pid)

    @staticmethod
    async def _until(ws, kind: str) -> None:
        async with asyncio.timeout(30):
            while json.loads(await ws.recv()).get("type") != kind:
                pass

    def exited_within(self, seconds: float) -> bool:
        try:
            self.process.wait(seconds)
        except subprocess.TimeoutExpired:
            return False
        finally:
            self._log.close()
        return True


async def started_with_a_service(rig: Rig, *, parent_pid: int = 0) -> tuple[Backend, int]:
    backend = Backend(rig, parent_pid=parent_pid)
    await backend.say("thank you, this is great")
    pid = rig.service_pid()
    assert running(pid)
    assert rig.service_homes(), "the service's home is under the backend's temp dir"
    return backend, pid


class TestTheBackendTakesItsServiceWithIt:
    @POSIX_ONLY
    async def test_when_its_window_quits(self, rig: Rig) -> None:
        window = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        rig.spawned.append(window.pid)
        backend, service = await started_with_a_service(rig, parent_pid=window.pid)
        window.kill()
        window.wait()
        assert backend.exited_within(30), backend.log_tail()
        # Not the home: the backend's watchdog may end it hard 5 s after its
        # window goes, before its exit handlers run; the service goes anyway.
        assert gone_within(service, GONE_S)

    @POSIX_ONLY
    @pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT], ids=["sigterm", "sigint"])
    async def test_when_it_is_signalled(self, rig: Rig, sig: signal.Signals) -> None:
        backend, service = await started_with_a_service(rig)
        backend.process.send_signal(sig)
        assert backend.exited_within(30), backend.log_tail()
        assert gone_within(service, GONE_S)
        assert rig.service_homes() == []

    async def test_when_it_is_killed_outright(self, rig: Rig) -> None:
        backend, service = await started_with_a_service(rig)
        backend.process.kill()
        assert backend.exited_within(10)
        assert gone_within(service, GONE_S)


# ── The service on its own ───────────────────────────────────────


class TestTheServiceStopsMidLoad:
    """Told to stop while Laya is still loading, it goes within its grace."""

    def start(self, rig: Rig, *extra: str, stdin=subprocess.PIPE) -> tuple[subprocess.Popen, int]:
        ready = rig.root / "port"
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "igni_core.decisions_server",
                "--ready-file",
                str(ready),
                *extra,
            ],
            env=rig.env,
            stdin=stdin,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        rig.spawned.append(process.pid)
        deadline = time.monotonic() + 60
        while not ready.exists():
            assert process.poll() is None, "the service exited as it started"
            assert time.monotonic() < deadline, "the service never wrote its ready file"
            time.sleep(0.05)
        return process, int(ready.read_text())

    @staticmethod
    def ask_in_the_background(port: int) -> None:
        def ask() -> None:
            with contextlib.suppress(httpx.HTTPError):
                httpx.post(
                    f"http://127.0.0.1:{port}/v1/predict",
                    json=QUESTION,
                    headers={"authorization": "Bearer s3cret"},
                    timeout=120,
                )

        threading.Thread(target=ask, daemon=True).start()

    def test_when_its_stdin_closes(self, rig: Rig) -> None:
        process, port = self.start(rig, "--watch-stdin")
        self.ask_in_the_background(port)
        assert rig.service_pid() == process.pid
        assert process.stdin is not None
        process.stdin.close()
        assert gone_within(process.pid, GONE_S)

    @POSIX_ONLY
    def test_when_it_is_sent_sigterm(self, rig: Rig) -> None:
        process, port = self.start(rig)
        self.ask_in_the_background(port)
        rig.service_pid()
        process.send_signal(signal.SIGTERM)
        assert gone_within(process.pid, GONE_S)

    def test_when_its_parent_dies_while_another_holds_its_stdin(self, rig: Rig) -> None:
        """The stdin lifeline can be held open by someone else; the parent watch still fires."""
        ready = rig.root / "port"
        parent = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import os, subprocess, sys, time\n"
                "subprocess.Popen([sys.executable, '-m', 'igni_core.decisions_server',"
                f" '--ready-file', {str(ready)!r}, '--watch-stdin',"
                " '--parent-pid', str(os.getpid())])\n"
                "time.sleep(600)\n",
            ],
            env=rig.env,
            stdin=subprocess.PIPE,
        )
        rig.spawned.append(parent.pid)
        deadline = time.monotonic() + 60
        while not ready.exists():
            assert time.monotonic() < deadline, "the service never wrote its ready file"
            time.sleep(0.05)
        self.ask_in_the_background(int(ready.read_text()))
        service = rig.service_pid()
        parent.kill()
        parent.wait()
        assert parent.stdin is not None and not parent.stdin.closed  # still held open, here
        assert gone_within(service, GONE_S)
        parent.stdin.close()


# ── The backend's side ───────────────────────────────────────────


class TestLocalDecisionsCloses:
    def test_close_stops_the_service_removes_its_home_and_is_registered_at_exit(
        self, rig: Rig, monkeypatch
    ) -> None:
        for key, value in rig.env.items():
            monkeypatch.setenv(key, value)
        registered: list[object] = []
        monkeypatch.setattr("atexit.register", registered.append)
        monkeypatch.setattr("tempfile.tempdir", str(rig.tmp))
        local = LocalDecisions()
        local.connection()
        process = local._process  # noqa: SLF001
        assert process is not None
        rig.spawned.append(process.pid)
        assert ["--parent-pid", str(os.getpid())] == process.args[-2:]  # type: ignore[index]
        assert "--watch-stdin" in process.args  # type: ignore[operator]
        assert registered == [local.close]
        process.kill()  # as if it had idled out: the next call starts another
        process.wait()
        local.connection()
        process = local._process  # noqa: SLF001
        assert process is not None
        rig.spawned.append(process.pid)
        assert registered == [local.close], "registered once, however often it starts"
        local.close()
        assert not running(process.pid)
        if sys.platform != "win32":
            assert process.returncode in (0, -signal.SIGTERM), "asked to stop, not killed"
        assert rig.service_homes() == []
        local.close()  # a second close has nothing left to do

    def test_close_kills_a_service_that_will_not_stop(self, rig: Rig, monkeypatch) -> None:
        for key, value in rig.env.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setattr("tempfile.tempdir", str(rig.tmp))
        local = LocalDecisions()
        local.connection()
        process = local._process  # noqa: SLF001
        assert process is not None
        rig.spawned.append(process.pid)
        if sys.platform != "win32":
            os.kill(process.pid, signal.SIGSTOP)  # deaf to SIGTERM
        monkeypatch.setattr(LocalDecisions, "STOP_S", 0.5)
        local.close()
        assert process.returncode is not None
        assert rig.service_homes() == []

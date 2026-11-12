"""A rig for running the ``scripts/profile_*.py`` probes without a real igni.

The probes spawn ``.venv/bin/python -m igni_core.backend``, talk to it
over a WebSocket and read ``ps`` / ``lsof``. The rig stands in for those
three things only, so the probe itself runs for real, as a subprocess,
and what a user would see on stdout and in the exit code is what is
asserted:

* ``PYTHON`` of the probe is a wrapper that records its argv and the
  environment it was given, then serves the welcome / rpc protocol on an
  ephemeral port after the ready line;
* ``ps`` and ``lsof`` on ``PATH`` print canned tables whose numbers follow
  the knobs in ``Rig.env``;
* ``asyncio.sleep`` and the probe's own clock run on a virtual timeline,
  so minutes of probe time take a moment.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# The old product name, spelled in pieces so the repository guard stays meaningful.
OLD_NAME = "em" + "ber"

_BACKEND = r"""
import asyncio, json, os, signal, sys, time
from pathlib import Path

import websockets

REC = Path(os.environ["RIG_DIR"])
MODE = os.environ.get("RIG_MODE", "normal")
DELAY = float(os.environ.get("RIG_RPC_DELAY", "0"))
# Each get_status takes STEP longer than the one before it.
STEP = float(os.environ.get("RIG_RPC_DELAY_STEP", "0"))
STATUS_CALLS = [0]


def note(name, value):
    with (REC / name).open("a") as fh:
        fh.write(json.dumps(value) + "\n")


(REC / "pid").write_text(str(os.getpid()))
note("start", {
    "argv": sys.argv[1:],
    "ppid": os.getppid(),
    "env": {k: v for k, v in os.environ.items() if k.startswith(("IGNI_", "PYTHON"))},
    "cwd": os.getcwd(),
})
for line in os.environ.get("RIG_STDERR", "").splitlines():
    print(line, file=sys.stderr, flush=True)
if MODE == "die":
    sys.exit(3)


def on_usr1(*_):
    with (REC / "usr1").open("a") as fh:
        fh.write("x\n")


signal.signal(signal.SIGUSR1, on_usr1)
if MODE == "stubborn":
    signal.signal(signal.SIGTERM, lambda *_: None)


async def handler(ws):
    if MODE == "badwelcome":
        await ws.send(json.dumps({"type": "error", "message": "no"}))
        await asyncio.sleep(30)
        return
    await ws.send(json.dumps({"type": "welcome", "client_id": "c"}))
    async for raw in ws:
        msg = json.loads(raw)
        note("rpcs", {"method": msg["method"], "args": msg["args"]})
        if msg["method"] == "attach_session":
            (REC / "attached").write_text("1")
        if MODE == "mute" and msg["method"] == "get_status":
            continue
        if MODE == "hangup" and msg["method"] == "get_status":
            await ws.close()
            return
        if msg["method"] == "get_status":
            STATUS_CALLS[0] += 1
        if DELAY or STEP:
            await asyncio.sleep(DELAY + STEP * STATUS_CALLS[0])
        await ws.send(json.dumps({"type": "rpc_response", "id": msg["id"], "result": {}}))


async def main():
    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        print("not json at all", flush=True)
        print(json.dumps({"status": "ready", "ws_url": f"ws://127.0.0.1:{port}"}), flush=True)
        # A chatty backend: more output after ready than a pipe buffer holds.
        # If the probe left a pipe undrained this blocks the whole loop.
        for i in range(int(os.environ.get("RIG_CHATTY_KB", "0"))):
            print(f"chatter {i} " + "x" * 1000, file=sys.stderr if i % 2 else sys.stdout, flush=True)
        await asyncio.Future()


asyncio.run(main())
"""

_PS = r"""#!{python}
import os, sys
from pathlib import Path

REC = Path(os.environ["RIG_DIR"])
args = sys.argv[1:]
pid = int((REC / "pid").read_text()) if (REC / "pid").exists() else 0
step = int(os.environ.get("RIG_PS_STEP", "0"))
base = int(os.environ.get("RIG_PS_BASE", "100000"))
if os.environ.get("RIG_PS_FAIL"):
    sys.exit(1)
if "RIG_PS_GC_DELTA_KB" in os.environ:
    # phase mode: boot < attached < after the SIGUSR1 sweep
    rss = int(os.environ["RIG_PS_BOOT_KB"])
    if (REC / "attached").exists():
        rss = int(os.environ["RIG_PS_ATTACHED_KB"])
    if (REC / "usr1").exists():
        rss += int(os.environ["RIG_PS_GC_DELTA_KB"])
else:
    counter = REC / "ps-calls"
    calls = int(counter.read_text()) if counter.exists() else 0
    if "rss=,pid=,ppid=" in args or "rss=" in args:
        counter.write_text(str(calls + 1))
    rss = base + calls * step
if "-p" in args:
    if args[args.index("-p") + 1] == str(pid):
        print(f"  {{rss}}")
    sys.exit(0)
rows = [(rss, pid, 1), (100, 90001, pid), (50, 90002, 90001), (999999, 90003, 1)]
for r, p, pp in rows:
    if "rss=,pid=,ppid=" in args:
        print(f"{{r}} {{p}} {{pp}}")
    else:
        print(f"{{p}} {{pp}}")
print("garbage")
print("x y z")
"""

_LSOF = r"""#!{python}
import os, sys
from pathlib import Path

REC = Path(os.environ["RIG_DIR"])
if os.environ.get("RIG_LSOF_HANG"):
    import time
    time.sleep(30)
counter = REC / "lsof-calls"
n = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(n))
cycle = max(0, (n - 2) // 4 + 1)
drift = int(os.environ.get("RIG_LSOF_DRIFT", "0"))
print("COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME")
print("python 1 u cwd DIR 1,1 1 1 /")
print("python 1 u txt REG 1,1 1 1 /bin/python")
print("python 1 u mem REG 1,1 1 1 /lib/x")
print("python 1 u NOFD")
print("short row")
for fd, kind in [(5, "IPv4"), (6, "IPv6"), (7, "unix"), (8, "PIPE"), (9, "REG"), (10, "DIR"), (11, "CHR")]:
    suffix = "r" if kind in ("PIPE", "DIR") else "u"
    print(f"python 1 u {{fd}}{{suffix}} {{kind}} 1,1 1 1 /x")
for i in range(int(os.environ.get("RIG_LSOF_EXTRA", "0")) + cycle * drift):
    print(f"python 1 u {{100 + i}}u PIPE 1,1 1 1 /p")
"""

_RUNNER = r"""
import asyncio, importlib.util, os, sys, time, types
from pathlib import Path

script, python, *rest = sys.argv[1:]
spec = importlib.util.spec_from_file_location("probe_under_test", script)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.PYTHON = Path(python)

offset = [0.0]
real_sleep = asyncio.sleep
cap = float(os.environ.get("RIG_REAL_SLEEP_CAP", "0.01"))


async def fast_sleep(seconds, *a, **k):
    # A phase or sampling wait (a second or more) is skipped on the virtual
    # clock; a short pacing pause stays real, so concurrent drivers do not
    # make the clock run away.
    if seconds >= 1:
        offset[0] += seconds
        await real_sleep(min(seconds, cap))
    else:
        await real_sleep(seconds)


mod.asyncio = types.SimpleNamespace(**{**vars(asyncio), "sleep": fast_sleep})
mod.time = types.SimpleNamespace(
    monotonic=lambda: time.monotonic() + offset[0],
    monotonic_ns=time.monotonic_ns,
)
sys.argv = [script, *rest]
sys.exit(mod.main())
"""


class Rig:
    """One probe run's stand-ins, kept under ``tmp_path``."""

    def __init__(self, tmp_path: Path):
        self.dir = tmp_path / "rig"
        self.bin = self.dir / "bin"
        self.bin.mkdir(parents=True)
        (self.dir / "backend.py").write_text(_BACKEND)
        self.python = self.dir / "python"
        self._exe(self.python, f'#!/bin/sh\nexec "{sys.executable}" "{self.dir}/backend.py" "$@"\n')
        (self.dir / "runner.py").write_text(_RUNNER)
        self._exe(self.bin / "ps", _PS.format(python=sys.executable))
        self._exe(self.bin / "lsof", _LSOF.format(python=sys.executable))
        self.env: dict[str, str] = {}
        self.path_dirs = [str(self.bin)]
        self.system_dirs = ["/usr/bin", "/bin"]

    @staticmethod
    def _exe(path: Path, text: str) -> None:
        path.write_text(text)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def run(self, script: str, *args: str, timeout: float = 120) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("IGNI_", "RIG_"))}
        env["PATH"] = os.pathsep.join([*self.path_dirs, *self.system_dirs])
        env["RIG_DIR"] = str(self.dir)
        env.update(self.env)
        return subprocess.run(
            [
                sys.executable,
                str(self.dir / "runner.py"),
                str(REPO / "scripts" / script),
                str(self.python),
                *args,
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
            cwd=REPO,
        )

    def reap(self) -> None:
        """Never leave a stand-in backend running, whatever the probe did."""
        pidfile = self.dir / "pid"
        if not pidfile.exists():
            return
        with contextlib.suppress(ProcessLookupError, ValueError):
            os.kill(int(pidfile.read_text()), signal.SIGKILL)

    def started(self) -> dict:
        return json.loads((self.dir / "start").read_text().splitlines()[0])

    def rpcs(self) -> list[dict]:
        path = self.dir / "rpcs"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def usr1_count(self) -> int:
        path = self.dir / "usr1"
        return len(path.read_text().splitlines()) if path.exists() else 0

    def backend_alive(self) -> bool:
        """True while the stand-in backend is running (waits briefly for it to go)."""
        pid = int((self.dir / "pid").read_text())
        for _ in range(30):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            time.sleep(0.1)
        return True


def dedent(text: str) -> str:
    return textwrap.dedent(text).strip()

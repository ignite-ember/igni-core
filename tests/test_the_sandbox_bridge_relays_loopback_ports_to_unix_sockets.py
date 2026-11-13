"""The Linux sandbox's bridge relays loopback ports to unix sockets and runs the command.

:mod:`igni_core.extensions.sandbox.bridge` is the first process inside
``bwrap --unshare-net``: the extension can reach nothing but its own
loopback, and the bridge listens there for each ``--forward PORT=SOCKET``
and relays every connection to a unix socket igni serves outside. The
real ``bwrap`` path is in
``test_an_extension_process_is_confined_to_its_grants.py`` and runs only
on Linux with bubblewrap. The bridge itself is standard library only and
does nothing Linux-specific, so it is checked here on any OS: called
in-process, with a unix socket this test serves and a short Python child
standing in for the extension.

``main`` installs signal handlers and leaves its listeners to the
process's exit — which, in-process, is the test runner's. So each test
restores the handlers, and the listeners are recorded on their way out
of ``socket.socket`` and closed afterwards, which ends their threads.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import types
from collections.abc import Iterator
from pathlib import Path

import pytest

from igni_core.extensions.sandbox import bridge

pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="needs unix sockets")


class _RecordingSocketModule(types.ModuleType):
    """The ``socket`` module as the bridge sees it, noting each socket it makes."""

    def __init__(self) -> None:
        super().__init__("socket")
        self.made: list[socket.socket] = []

    def __getattr__(self, name: str) -> object:
        return getattr(socket, name)

    def socket(self, *args: object, **kwargs: object) -> socket.socket:  # type: ignore[override]
        made = socket.socket(*args, **kwargs)  # type: ignore[arg-type]
        self.made.append(made)
        return made


@pytest.fixture
def bridged(monkeypatch) -> Iterator[_RecordingSocketModule]:
    """Run the bridge in-process; afterwards put back what it changed."""
    recorder = _RecordingSocketModule()
    monkeypatch.setattr(bridge, "socket", recorder)
    handlers = {
        sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
    }
    try:
        yield recorder
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        for made in recorder.made:
            with contextlib.suppress(OSError):
                made.shutdown(socket.SHUT_RDWR)
            made.close()


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """A directory short enough for a unix socket path (104 bytes on macOS)."""
    path = Path(tempfile.mkdtemp(prefix="br", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _serve_unix(path: Path, reply: bytes) -> threading.Thread:
    """A unix-socket server that answers one connection: ``reply`` + what it was sent."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)

    def answer() -> None:
        with server:
            conn, _ = server.accept()
            with conn:
                received = b""
                while chunk := conn.recv(1024):
                    received += chunk
                conn.sendall(reply + received)

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    return thread


def _child(code: str) -> list[str]:
    return [sys.executable, "-c", textwrap.dedent(code)]


def test_a_connection_to_the_forwarded_port_reaches_the_unix_socket(bridged, short_dir):
    port = _free_port()
    upstream = short_dir / "proxy.sock"
    server = _serve_unix(upstream, b"proxy saw: ")
    out = short_dir / "answer"
    command = _child(
        f"""
        import socket
        with socket.create_connection(("127.0.0.1", {port}), timeout=10) as conn:
            conn.sendall(b"CONNECT example.com:443")
            conn.shutdown(socket.SHUT_WR)
            answer = b""
            while chunk := conn.recv(1024):
                answer += chunk
        open({str(out)!r}, "wb").write(answer)
        """
    )

    code = bridge.main(["--forward", f"{port}={upstream}", "--", *command])

    server.join(10)
    assert code == 0
    assert out.read_bytes() == b"proxy saw: CONNECT example.com:443"


def test_a_forward_to_a_socket_nobody_serves_closes_the_connection(bridged, short_dir):
    port = _free_port()
    out = short_dir / "answer"
    command = _child(
        f"""
        import socket
        with socket.create_connection(("127.0.0.1", {port}), timeout=10) as conn:
            open({str(out)!r}, "wb").write(conn.recv(1024))
        """
    )

    code = bridge.main(["--forward", f"{port}={short_dir / 'absent.sock'}", "--", *command])

    assert code == 0
    assert out.read_bytes() == b""


def test_the_commands_exit_status_is_the_bridges(bridged):
    assert bridge.main(["--", *_child("raise SystemExit(7)")]) == 7


def test_a_signal_is_passed_to_the_command_and_reported_as_128_plus_it(bridged, short_dir):
    ready = short_dir / "ready"
    command = _child(
        f"""
        import time
        open({str(ready)!r}, "w").close()
        time.sleep(30)
        """
    )

    def stop_when_ready() -> None:
        for _ in range(1000):
            if ready.exists():
                os.kill(os.getpid(), signal.SIGTERM)
                return
            threading.Event().wait(0.01)

    killer = threading.Thread(target=stop_when_ready, daemon=True)
    killer.start()

    code = bridge.main(["--", *command])

    killer.join(5)
    assert code == 128 + signal.SIGTERM


@pytest.mark.parametrize(
    ("argv", "says"),
    [
        (["python"], "usage: bridge.py"),
        (["--listen", "1=/x", "--", "true"], "unknown option --listen"),
    ],
)
def test_a_malformed_command_line_is_refused_with_status_2(bridged, capsys, argv, says):
    assert bridge.main(argv) == 2
    assert says in capsys.readouterr().err


def test_no_command_after_the_separator_is_status_2(bridged):
    assert bridge.main(["--"]) == 2


@pytest.mark.parametrize(
    "forward", [["--forward"], ["--forward", "web=/x.sock"], ["--forward", "=/x.sock"]]
)
def test_a_forward_without_a_port_number_is_status_2_not_a_traceback(bridged, capsys, forward):
    assert bridge.main([*forward, "--", "true"]) == 2
    assert "--forward needs PORT=SOCKET" in capsys.readouterr().err


def test_a_port_that_cannot_be_listened_on_is_status_1_and_names_it(bridged, capsys):
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen(1)
    port = taken.getsockname()[1]
    try:
        # Without SO_REUSEADDR sharing a listening port, the bind is refused.
        assert bridge.main(["--forward", f"{port}=/x.sock", "--", "true"]) == 1
    finally:
        taken.close()
    assert f"cannot listen on 127.0.0.1:{port}" in capsys.readouterr().err


def test_a_command_that_does_not_exist_is_status_127_and_named(bridged, capsys, short_dir):
    assert bridge.main(["--", str(short_dir / "no-such-program")]) == 127
    assert "no-such-program" in capsys.readouterr().err


def test_a_command_that_cannot_be_run_is_status_126(bridged, capsys, short_dir):
    not_runnable = short_dir / "plain-file"
    not_runnable.write_text("x")
    assert bridge.main(["--", str(not_runnable)]) == 126
    assert "cannot run" in capsys.readouterr().err


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGHUP])
def test_sigint_and_sighup_are_passed_to_the_command_too(bridged, short_dir, sig):
    ready = short_dir / "ready"
    command = _child(
        f"""
        import time
        open({str(ready)!r}, "w").close()
        time.sleep(30)
        """
    )
    command[2] = "import signal\nsignal.signal(signal.SIGINT, signal.SIG_DFL)\n" + command[2]

    def stop_when_ready() -> None:
        for _ in range(1000):
            if ready.exists():
                os.kill(os.getpid(), sig)
                return
            threading.Event().wait(0.01)

    killer = threading.Thread(target=stop_when_ready, daemon=True)
    killer.start()

    code = bridge.main(["--", *command])

    killer.join(5)
    assert code == 128 + sig


def test_two_forwards_each_reach_their_own_socket(bridged, short_dir):
    first, second = _free_port(), _free_port()
    servers = [
        _serve_unix(short_dir / "a.sock", b"A:"),
        _serve_unix(short_dir / "b.sock", b"B:"),
    ]
    out = short_dir / "answer"
    command = _child(
        f"""
        import socket
        got = []
        for port in ({first}, {second}):
            with socket.create_connection(("127.0.0.1", port), timeout=10) as conn:
                conn.sendall(b"x")
                conn.shutdown(socket.SHUT_WR)
                data = b""
                while chunk := conn.recv(1024):
                    data += chunk
                got.append(data)
        open({str(out)!r}, "wb").write(b"|".join(got))
        """
    )

    code = bridge.main(
        [
            "--forward",
            f"{first}={short_dir / 'a.sock'}",
            "--forward",
            f"{second}={short_dir / 'b.sock'}",
            "--",
            *command,
        ]
    )

    for server in servers:
        server.join(10)
    assert code == 0
    assert out.read_bytes() == b"A:x|B:x"


def test_more_than_one_chunk_goes_through_whole_and_in_order(bridged, short_dir):
    port = _free_port()
    server = _serve_unix(short_dir / "p.sock", b"")
    payload = bytes(range(256)) * 1024  # 256 KiB: several 64 KiB reads
    out = short_dir / "answer"
    command = _child(
        f"""
        import socket
        payload = bytes(range(256)) * 1024
        with socket.create_connection(("127.0.0.1", {port}), timeout=20) as conn:
            conn.sendall(payload)
            conn.shutdown(socket.SHUT_WR)
            data = b""
            while chunk := conn.recv(65536):
                data += chunk
        open({str(out)!r}, "wb").write(data)
        """
    )

    code = bridge.main(["--forward", f"{port}={short_dir / 'p.sock'}", "--", *command])

    server.join(20)
    assert code == 0
    assert out.read_bytes() == payload


def test_the_end_of_one_side_is_passed_on_so_a_reply_can_still_come_back(bridged, short_dir):
    """A client that has finished sending still reads the answer: the half-close
    is relayed as a shutdown of the write side only."""
    port = _free_port()
    upstream = short_dir / "h.sock"
    server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server_socket.bind(str(upstream))
    server_socket.listen(1)
    ended_before_reply: list[bool] = []

    def serve() -> None:
        with server_socket:
            conn, _ = server_socket.accept()
            with conn:
                # Reading until EOF only ends if the bridge relayed the client's shutdown.
                while conn.recv(1024):
                    pass
                ended_before_reply.append(True)
                conn.sendall(b"late answer")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    out = short_dir / "answer"
    command = _child(
        f"""
        import socket
        with socket.create_connection(("127.0.0.1", {port}), timeout=10) as conn:
            conn.sendall(b"request")
            conn.shutdown(socket.SHUT_WR)
            data = b""
            while chunk := conn.recv(1024):
                data += chunk
        open({str(out)!r}, "wb").write(data)
        """
    )

    code = bridge.main(["--forward", f"{port}={upstream}", "--", *command])

    thread.join(10)
    assert code == 0
    assert ended_before_reply == [True]
    assert out.read_bytes() == b"late answer"


def test_run_as_a_script_it_works_under_umask_077_and_exits_with_the_commands_status():
    script = Path(bridge.__file__)
    done = subprocess.run(  # noqa: S603 — igni's own interpreter and script
        [
            sys.executable,
            "-I",
            str(script),
            "--",
            sys.executable,
            "-c",
            "import os, sys; mask = os.umask(0); print(oct(mask)); sys.exit(5)",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 5
    assert done.stdout.strip() == "0o77"

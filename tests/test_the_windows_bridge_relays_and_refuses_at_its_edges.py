"""The Windows bridge's relay, its arguments and its Win32 calls, at their edges.

``winbridge.py`` is the script that gives an AppContainer its way out. Its
real run needs Windows (``test_an_extension_process_is_confined_on_windows.py``
runs it there); everything below runs anywhere: the relay over real
loopback sockets, the shim over a real child process, and the Win32 calls
through a faked boundary that records what each call was handed.
"""

from __future__ import annotations

import asyncio
import ctypes
import sys
from pathlib import Path
from typing import Any

import pytest

from igni_core.extensions.sandbox import winbridge

pytestmark = pytest.mark.extension_sandbox

PIPE = "\\\\.\\pipe\\igni-x-3128"


# ── Arguments ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    ["", "3128", "3128=", "=" + PIPE, "abc=" + PIPE, "-1=" + PIPE, "31 28=" + PIPE, "3128=/tmp/s"],
)
def test_a_forward_is_a_whole_port_and_a_pipe_under_the_pipe_prefix(text):
    with pytest.raises(ValueError, match="--forward wants"):
        winbridge.parse_forward(text)


def test_a_forward_keeps_an_equals_sign_inside_the_pipe_name():
    assert winbridge.parse_forward("007=" + PIPE + "=a") == (7, PIPE + "=a")


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["run"],
        ["run", "x"],  # no separator
        ["run", "--"],  # no command
        ["shim", "--sid", "--", "x"],  # an option without its value
        ["shim", "sid", "v", "--", "x"],  # a name that is not an option
        ["fly", "--", "x"],
        ["--", "x"],
    ],
)
def test_arguments_without_a_mode_pairs_and_a_command_are_refused(argv):
    with pytest.raises(ValueError):
        winbridge.parse_args(argv)


def test_a_repeated_option_keeps_every_value_in_order_and_the_command_keeps_its_dashes():
    mode, options, command = winbridge.parse_args(
        [
            "shim",
            "--forward",
            "1=" + PIPE,
            "--forward",
            "2=" + PIPE,
            "--cwd",
            "d",
            "--",
            "py",
            "--",
            "-V",
        ]
    )
    assert mode == "shim"
    assert options == {"--forward": ["1=" + PIPE, "2=" + PIPE], "--cwd": ["d"]}
    assert command == ["py", "--", "-V"]


def test_what_a_run_cannot_start_without_is_refused_before_anything_starts():
    full = [
        "run",
        "--sid",
        "S-1-15-2-9",
        "--user",
        "S-1-5-21-1",
        "--python",
        "py",
        "--parent",
        "42",
    ]
    for left_out in ("--sid", "--user", "--python", "--parent"):
        at = full.index(left_out)
        argv = [*full[:at], *full[at + 2 :], "--", "x"]
        with pytest.raises(ValueError, match=left_out):
            _check(argv)
    with pytest.raises(ValueError, match="process id"):
        _check([*full[:-1], "four", "--", "x"])
    with pytest.raises(ValueError, match="--forward wants"):
        _check([*full, "--forward", "abc=" + PIPE, "--", "x"])
    with pytest.raises(ValueError, match="--setenv wants"):
        _check(["shim", "--setenv", "PATH=C:\\evil", "--", "x"])
    _check([*full, "--forward", "1=" + PIPE, "--", "x"])  # all there: nothing said


def _check(argv: list[str]) -> None:
    mode, options, _ = winbridge.parse_args(argv)
    winbridge.check_options(mode, options)


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "--sid", "S-1", "--", "x"],
        ["run", "--forward", "abc=" + PIPE, "--", "x"],
        ["shim", "--forward", "abc=" + PIPE, "--", "x"],
        ["shim", "--setenv", "PATH=C:\\evil", "--", "x"],
        ["shim", "--setenv", "TEMP", "--", "x"],
    ],
)
def test_a_bad_argument_is_one_line_and_status_2_not_a_traceback(argv, capsys, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert winbridge.main(argv) == 2
    err = capsys.readouterr().err
    assert err.startswith("winbridge.py: ") and err.count("\n") == 1


def test_off_windows_the_bridge_says_so_for_arguments_that_are_right(capsys, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    argv = ["shim", "--forward", "1=" + PIPE, "--", "x"]
    assert winbridge.main(argv) == 2
    assert "this runs on Windows only" in capsys.readouterr().err


# ── The environment ───────────────────────────────────────────────────


def test_only_set_and_non_empty_file_locations_are_kept():
    env = {"TEMP": "C:\\t", "TMP": "", "HOME": "C:\\h", "PATH": "x", "APPDATA": "C:\\a"}
    assert winbridge.kept_env(env) == {"TEMP": "C:\\t", "HOME": "C:\\h", "APPDATA": "C:\\a"}
    assert winbridge.kept_env({}) == {}


def test_a_shim_argv_names_every_kept_variable_after_the_forwards_and_before_the_command():
    argv = winbridge.shim_argv(
        "py", "wb.py", [(1, PIPE)], ["x", "--", "y"], {"TEMP": "a=b", "TMP": "c"}
    )
    assert argv == [
        "py", "-I", "wb.py", "shim", "--forward", f"1={PIPE}",
        "--setenv", "TEMP=a=b", "--setenv", "TMP=c", "--", "x", "--", "y",
    ]  # fmt: skip
    mode, options, command = winbridge.parse_args(argv[3:])
    assert command == ["x", "--", "y"]
    assert winbridge.restore_env({}, options["--setenv"]) == {"TEMP": "a=b", "TMP": "c"}


def test_a_restored_value_may_hold_equals_signs_and_an_item_without_one_is_refused():
    assert winbridge.restore_env({"A": "1"}, ["HOME=a=b", "TEMP="]) == {
        "A": "1",
        "HOME": "a=b",
        "TEMP": "",
    }
    env = {"TEMP": "x"}
    for bad in ("TEMP", "temp=x", "PATH=x", "=x"):
        with pytest.raises(ValueError, match="--setenv wants"):
            winbridge.restore_env(env, [bad])
    assert env == {"TEMP": "x"}  # the caller's own is never changed


def test_every_loopback_spelling_is_rewritten_and_whole_ports_only():
    ports = {4100: 50000}
    env = {
        "A": "http://127.0.0.1:4100/p",
        "B": "ws://localhost:4100",
        "C": "http://[::1]:4100",
        "D": "127.0.0.1:4100,localhost:4100",
        "NEXT": "127.0.0.1:41001 127.0.0.1:410",
        "WORD": "x127.0.0.1:4100 10.127.0.0.1:4100 a.localhost:4100",
        "UPPER": "http://LOCALHOST:4100",
        "OTHERHOST": "http://example.com:4100 127.0.0.2:4100",
        "NONE": "",
    }
    out = winbridge.rewrite_ports(env, ports)
    assert out["A"] == "http://127.0.0.1:50000/p"
    assert out["B"] == "ws://127.0.0.1:50000"
    assert out["C"] == "http://127.0.0.1:50000"
    assert out["D"] == "127.0.0.1:50000,127.0.0.1:50000"
    for untouched in ("NEXT", "WORD", "UPPER", "OTHERHOST", "NONE"):
        assert out[untouched] == env[untouched]
    assert winbridge.rewrite_ports(env, {}) == env


def test_a_port_already_rewritten_is_not_rewritten_again_for_another_forward():
    # The shim's own port for one forward may equal igni's port for another.
    env = {"A": "http://127.0.0.1:3128", "B": "http://127.0.0.1:41001"}
    out = winbridge.rewrite_ports(env, {3128: 41001, 41001: 50001})
    assert out == {"A": "http://127.0.0.1:41001", "B": "http://127.0.0.1:50001"}


def test_the_pipe_security_is_exactly_system_person_container_and_a_low_label():
    sddl = winbridge.pipe_sddl("S-1-5-21-1-2-3-1001", "S-1-15-2-9")
    assert sddl == (
        "D:P(A;;GA;;;SY)(A;;GA;;;S-1-5-21-1-2-3-1001)(A;;GRGW;;;S-1-15-2-9)S:(ML;;NW;;;LW)"
    )


# ── The relay, over real sockets ──────────────────────────────────────


async def _linked() -> tuple[Any, Any, Any, Any, Any]:
    """Two socket pairs spliced together: ``client_a <-> splice <-> client_b``."""
    sides: dict[str, tuple] = {}
    ready = {name: asyncio.Event() for name in ("a", "b")}

    def serving(name):
        async def accepted(reader, writer):
            sides[name] = (reader, writer)
            ready[name].set()

        return accepted

    server_a = await asyncio.start_server(serving("a"), "127.0.0.1", 0)
    server_b = await asyncio.start_server(serving("b"), "127.0.0.1", 0)
    client_a = await asyncio.open_connection("127.0.0.1", server_a.sockets[0].getsockname()[1])
    client_b = await asyncio.open_connection("127.0.0.1", server_b.sockets[0].getsockname()[1])
    await asyncio.wait_for(asyncio.gather(ready["a"].wait(), ready["b"].wait()), 5)
    splice = asyncio.create_task(winbridge._splice(sides["a"], sides["b"]))
    return client_a, client_b, splice, server_a, server_b


async def test_the_relay_carries_bytes_both_ways_over_more_than_one_chunk():
    client_a, client_b, splice, *servers = await _linked()
    try:
        big = bytes(range(256)) * 1024  # 256 KiB, four chunks of 64 KiB
        client_a[1].write(big)
        await client_a[1].drain()
        assert await asyncio.wait_for(client_b[0].readexactly(len(big)), 5) == big
        client_b[1].write(b"back")
        await client_b[1].drain()
        assert await asyncio.wait_for(client_a[0].readexactly(4), 5) == b"back"
    finally:
        client_a[1].close()
        client_b[1].close()
        await asyncio.wait_for(splice, 5)
        for server in servers:
            server.close()


async def test_one_side_ending_is_told_to_the_other_which_may_still_answer():
    client_a, client_b, splice, *servers = await _linked()
    try:
        client_a[1].write(b"last words")
        client_a[1].write_eof()
        assert await asyncio.wait_for(client_b[0].read(), 5) == b"last words"  # then EOF
        client_b[1].write(b"still heard")  # the other direction stays open
        await client_b[1].drain()
        assert await asyncio.wait_for(client_a[0].readexactly(11), 5) == b"still heard"
        client_b[1].write_eof()
        assert await asyncio.wait_for(client_a[0].read(), 5) == b""
        await asyncio.wait_for(splice, 5)  # both ended: the relay returns
        assert await asyncio.wait_for(client_a[0].read(), 5) == b""
    finally:
        client_a[1].close()
        client_b[1].close()
        for server in servers:
            server.close()


async def test_a_reset_connection_is_told_to_the_other_end_and_never_raises():
    client_a, client_b, splice, *servers = await _linked()
    try:
        client_a[1].transport.abort()  # RST
        assert await asyncio.wait_for(client_b[0].read(), 5) == b""  # told as an end
        client_b[1].write(b"to nobody")  # the other way may fail; that is swallowed
        client_b[1].close()
        await asyncio.wait_for(splice, 5)  # returns, does not raise
    finally:
        client_b[1].close()
        for server in servers:
            server.close()


async def test_a_relay_that_cannot_write_to_its_writer_still_closes_both_writers():
    class Writer:
        closed = False

        def can_write_eof(self):
            raise RuntimeError("gone")

        def write(self, _):
            raise ConnectionResetError

        async def drain(self):
            raise ConnectionResetError

        def close(self):
            self.closed = True

    reader = asyncio.StreamReader()
    reader.feed_data(b"x")
    reader.feed_eof()
    other = asyncio.StreamReader()
    other.feed_eof()
    a, b = Writer(), Writer()
    await winbridge._splice((reader, a), (other, b))  # type: ignore[arg-type]
    assert a.closed and b.closed


# ── The shim, with a real child ───────────────────────────────────────


def _shim(tmp_path: Path, *args: str, monkeypatch, env: dict[str, str] | None = None) -> int:
    monkeypatch.setattr(sys, "platform", "win32")
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    return winbridge.main(["shim", *args])


def test_the_shim_restores_the_run_s_locations_rewrites_ports_and_passes_the_status_on(
    tmp_path, monkeypatch
):
    out = tmp_path / "seen.txt"
    code = (
        "import os,sys;"
        f"open({str(out)!r},'w').write(os.environ['TEMP']+'|'+os.environ['IGNI_URL']);"
        "sys.exit(7)"
    )
    status = _shim(
        tmp_path,
        "--setenv", f"TEMP={tmp_path}",
        "--", sys.executable, "-c", code,
        monkeypatch=monkeypatch,
        env={"TEMP": "C:\\container\\Temp", "IGNI_URL": "http://127.0.0.1:3128"},
    )  # fmt: skip
    assert status == 7
    temp, url = out.read_text().split("|")
    assert temp == str(tmp_path) and url == "http://127.0.0.1:3128"  # no forwards: no rewrite


def test_a_command_that_is_missing_or_not_runnable_is_one_line_and_the_shell_s_status(
    tmp_path, monkeypatch, capsys
):
    missing = _shim(tmp_path, "--", str(tmp_path / "nope"), monkeypatch=monkeypatch)
    assert missing == 127
    err = capsys.readouterr().err
    assert err.startswith("winbridge.py: cannot run ") and err.count("\n") == 1
    not_runnable = tmp_path / "plain.txt"
    not_runnable.write_text("x")
    not_runnable.chmod(0o644)
    assert _shim(tmp_path, "--", str(not_runnable), monkeypatch=monkeypatch) == 126


def test_a_port_that_cannot_be_served_is_one_line_and_status_1(monkeypatch, capsys):
    async def refuse(options, command):
        raise PermissionError("pipe taken")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(winbridge, "_run", refuse)
    argv = ["run", "--sid", "S", "--user", "U", "--python", "py", "--parent", "1", "--", "x"]
    assert winbridge.main(argv) == 1
    assert capsys.readouterr().err == "winbridge.py: pipe taken\n"


# ── The Win32 calls, through a faked boundary ─────────────────────────


class Win32:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.results: dict[str, Any] = {}
        win = self

        def fn(dll: str, name: str, restype: Any, *argtypes: Any):
            def call(*args):
                win.calls.append((name, args))
                result = win.results.get(name, 1)
                return result(*args) if callable(result) else result

            return call

        monkeypatch.setattr(winbridge, "_fn", fn)
        monkeypatch.setattr(ctypes, "WinError", lambda code, what: OSError(what), raising=False)
        monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)

    def named(self, name: str) -> list[tuple]:
        return [args for fn, args in self.calls if fn == name]


@pytest.fixture
def win32(monkeypatch):
    return Win32(monkeypatch)


@pytest.mark.parametrize("handle", [None, 0, -1, 2**64 - 1, 2**32 - 1])
def test_the_values_win32_gives_for_no_handle_are_not_handles(handle):
    assert winbridge._valid(handle) is False
    assert winbridge._valid(4)


def test_a_handle_is_closed_once_and_only_when_there_is_one(win32):
    winbridge._close(None)
    winbridge._close(-1)
    assert win32.calls == []
    winbridge._close(8)
    assert win32.named("CloseHandle") == [(8,)]


def test_a_job_kills_on_close_lets_nothing_break_away_and_holds_back_the_desktop(win32):
    seen: dict[str, Any] = {}

    def set_info(handle, kind, pointer, size):
        seen[kind] = (ctypes.cast(pointer, ctypes.c_void_p).value, size)
        if kind == winbridge.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION:
            limits = ctypes.cast(
                pointer, ctypes.POINTER(winbridge.JOBOBJECT_EXTENDED_LIMIT_INFORMATION)
            ).contents
            seen["flags"] = limits.BasicLimitInformation.LimitFlags
        else:
            seen["ui"] = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32)).contents.value
        return 1

    win32.results["CreateJobObjectW"] = 77
    win32.results["SetInformationJobObject"] = set_info
    job = winbridge.Job()
    assert job.handle == 77
    assert seen["flags"] == 0x2000 | 0x400  # kill on close, die on unhandled exception
    assert not seen["flags"] & 0x800  # never JOB_OBJECT_LIMIT_BREAKAWAY_OK
    assert not seen["flags"] & 0x1000  # nor silent breakaway
    assert seen["ui"] == winbridge.UI_RESTRICTIONS == 0xFE
    job.assign(5)
    assert win32.named("AssignProcessToJobObject") == [(77, 5)]
    job.close()
    assert win32.named("CloseHandle") == [(77,)] and job.handle is None


def test_a_job_that_cannot_be_made_or_limited_is_an_error_not_a_job_without_limits(win32):
    win32.results["CreateJobObjectW"] = 0
    with pytest.raises(OSError, match="CreateJobObjectW"):
        winbridge.Job()
    win32.results["CreateJobObjectW"] = 9
    win32.results["SetInformationJobObject"] = 0
    with pytest.raises(OSError, match="limits"):
        winbridge.Job()


def test_a_process_is_started_suspended_resumed_only_after_the_job_has_it(win32):
    job = winbridge.Job()
    winbridge.start_in_container("S-1-15-2-9", ["py", "-V", "a b"], "C:\\w", job)
    names = [name for name, _ in win32.calls]
    assert names.index("AssignProcessToJobObject") < names.index("ResumeThread")
    ((_, line, *_, flags, _env, cwd, _startup, _info),) = win32.named("CreateProcessW")
    assert line.value == 'py -V "a b"'
    assert flags == 0x80000 | 0x4  # extended startup info, suspended
    assert cwd == "C:\\w"
    assert len(win32.named("DeleteProcThreadAttributeList")) == 1
    assert len(win32.named("LocalFree")) == 1  # the SID


def test_a_process_that_cannot_join_the_job_is_ended_and_never_resumed(win32):
    win32.results["AssignProcessToJobObject"] = 0
    job = winbridge.Job()
    with pytest.raises(OSError, match="AssignProcessToJobObject"):
        winbridge.start_in_container("S-1-15-2-9", ["py"], None, job)
    assert len(win32.named("TerminateProcess")) == 1
    assert win32.named("TerminateProcess")[0][1] == 1
    assert win32.named("ResumeThread") == []
    assert len(win32.named("DeleteProcThreadAttributeList")) == 1
    assert len(win32.named("LocalFree")) == 1


def test_a_process_that_cannot_be_created_frees_what_it_took_and_says_where(win32):
    win32.results["CreateProcessW"] = 0
    with pytest.raises(OSError, match="CreateProcessW in AppContainer S-1-15-2-9"):
        winbridge.start_in_container("S-1-15-2-9", ["py"], None, winbridge.Job())
    assert win32.named("AssignProcessToJobObject") == []
    assert len(win32.named("LocalFree")) == 1


def test_waiting_is_for_the_process_and_igni_whichever_goes_first(win32):
    def wait(count, array, all_, timeout):
        win32.waited = (count, timeout)
        return win32.which

    def exit_code(process, pointer):
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32)).contents.value = 42
        return 1

    win32.results["WaitForMultipleObjects"] = wait
    win32.results["GetExitCodeProcess"] = exit_code
    win32.which = 0  # the process
    assert winbridge._wait(10, 20) == 42
    assert win32.waited == (2, winbridge.INFINITE)
    win32.which = 1  # igni
    win32.calls.clear()
    assert winbridge._wait(10, 20) == 1
    assert win32.named("GetExitCodeProcess") == []  # the process's code is not asked for
    win32.which = 0xFFFFFFFF  # the wait itself failed
    assert winbridge._wait(10, 20) == 1


@pytest.mark.parametrize("parent", [None, 0, -1])
def test_with_no_igni_to_watch_only_the_process_is_waited_on(win32, parent):
    def wait(count, array, all_, timeout):
        win32.count = count
        return 0

    win32.results["WaitForMultipleObjects"] = wait
    win32.results["GetExitCodeProcess"] = lambda process, pointer: 1
    assert winbridge._wait(10, parent) == 0
    assert win32.count == 1


def test_igni_is_opened_for_waiting_and_reading_its_status_only(win32):
    winbridge._open_parent(123)
    assert win32.named("OpenProcess") == [(0x100000 | 0x1000, False, 123)]


def test_a_standard_handle_is_made_inheritable_and_a_missing_one_is_passed_as_none(win32):
    handles = {0xFFFFFFF6: 11, 0xFFFFFFF5: 12, 0xFFFFFFF4: None}
    win32.results["GetStdHandle"] = lambda which: handles[which]
    assert winbridge._inheritable_stdio() == [11, 12, None]
    assert win32.named("SetHandleInformation") == [(11, 1, 1), (12, 1, 1)]


def test_a_closed_stdio_descriptor_is_passed_as_none(tmp_path):
    with (tmp_path / "f").open("w") as handle:
        fd = handle.fileno()
        assert winbridge._open_fd(fd) is True
    assert winbridge._open_fd(fd) is False

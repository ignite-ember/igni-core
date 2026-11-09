"""Windows: the two ends of an AppContainer's way out — a runner outside, a shim inside.

An AppContainer with no network capability reaches no address at all,
and loopback to a process outside its container is blocked too; that is
the point. What it must still reach — igni's egress proxy and igni's
two loopback endpoints (host channel, token endpoint) — it reaches
through named pipes, with two halves of this one script:

``run`` (outside, as the person, the command igni's MCP client starts)
    Creates a job object that kills everything in it when its handle
    closes and lets nothing break away; opens one named pipe per
    forwarded port, whose access list admits the person's account and
    the container's SID and nothing else, at low integrity so the
    container can write to it; relays each pipe connection to that
    port on this machine's ``127.0.0.1``; starts the shim inside the
    AppContainer (``SECURITY_CAPABILITIES`` with no capabilities), in
    the job, with exactly its own three stdio handles inherited; and
    exits with the shim's status — or kills the job when igni, its
    parent, is gone. Its exit closes the job, so nothing the extension
    started outlives the run.

``shim`` (inside the container, the process the job starts)
    For each forwarded port listens on a port of its own on
    ``127.0.0.1`` — a process in an AppContainer may use loopback
    within its own container — and relays each connection to that
    port's pipe. Then it rewrites the environment, replacing each
    forwarded ``127.0.0.1:PORT`` (``localhost``, ``[::1]``) with its
    own port, so ``HTTP(S)_PROXY`` and igni's endpoint URLs lead to
    it, and runs the extension's command with the same stdio, in the
    same container and job.

Why a shim rather than telling the process the pipe names: clients find
a proxy through ``HTTP_PROXY``, a URL, and an extension reaches igni's
host channel through a ``ws://`` URL; neither can name a pipe. Ports are
the shim's own, picked when it starts, because the container shares
this machine's loopback numbering with igni (unlike Linux's namespace,
:mod:`.bridge`), so igni's own port numbers are taken.

Standard library only, and no imports from igni: it runs under igni's
interpreter with ``-I``, from a copy in the run directory the container
may read. Importable anywhere; the Windows calls are made only when
``run`` or ``shim`` runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import functools
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from ctypes import wintypes
from typing import Any

#: Where every named pipe lives.
PIPE_PREFIX = "\\\\.\\pipe\\"

# ── What does not need Windows: arguments, access lists, the environment ──


def pipe_sddl(user_sid: str, container_sid: str) -> str:
    """The pipe's security: SYSTEM and the person in full, the container read/write.

    An AppContainer's access is checked twice — as its account and as
    its container — and both must allow, so the person's account is
    named as well as the container. ``P`` stops anything being
    inherited; the low mandatory label lets the container, which runs at
    low integrity, write. No other container, and no other account,
    may open it.
    """
    return f"D:P(A;;GA;;;SY)(A;;GA;;;{user_sid})(A;;GRGW;;;{container_sid})S:(ML;;NW;;;LW)"


def parse_forward(text: str) -> tuple[int, str]:
    """``PORT=PIPE`` as a pair."""
    port, sep, pipe = text.partition("=")
    if not sep or not port.isdigit() or not pipe.startswith(PIPE_PREFIX):
        raise ValueError(f"--forward wants PORT={PIPE_PREFIX}NAME, not {text!r}")
    return int(port), pipe


def parse_args(argv: list[str]) -> tuple[str, dict[str, list[str]], list[str]]:
    """``MODE [--option VALUE]... -- command [args]`` as its three parts."""
    if not argv or "--" not in argv:
        raise ValueError("usage: winbridge.py run|shim [--option VALUE]... -- command [args]")
    mode, rest = argv[0], argv[1:]
    split = rest.index("--")
    options: dict[str, list[str]] = {}
    pairs = rest[:split]
    if len(pairs) % 2:
        raise ValueError(f"an option without a value in {pairs}")
    for name, value in zip(pairs[::2], pairs[1::2], strict=True):
        if not name.startswith("--"):
            raise ValueError(f"not an option: {name}")
        options.setdefault(name, []).append(value)
    command = rest[split + 1 :]
    if mode not in ("run", "shim") or not command:
        raise ValueError("usage: winbridge.py run|shim [--option VALUE]... -- command [args]")
    return mode, options, command


def shim_argv(
    python: str, script: str, forwards: list[tuple[int, str]], command: list[str]
) -> list[str]:
    """The argv the runner starts inside the container."""
    argv = [python, "-I", script, "shim"]
    for port, pipe in forwards:
        argv += ["--forward", f"{port}={pipe}"]
    return [*argv, "--", *command]


_LOOPBACK = r"(?:127\.0\.0\.1|localhost|\[::1\])"


def rewrite_ports(env: Mapping[str, str], ports: Mapping[int, int]) -> dict[str, str]:
    """``env`` with each ``<loopback>:OLD`` written ``127.0.0.1:NEW``.

    Whole port numbers only: ``127.0.0.1:4100`` is not part of
    ``127.0.0.1:41001``.
    """
    out = dict(env)
    for old, new in ports.items():
        pattern = re.compile(rf"(?<![\w.]){_LOOPBACK}:{old}(?!\d)")
        for key, value in out.items():
            out[key] = pattern.sub(f"127.0.0.1:{new}", value)
    return out


# ── Relaying ──────────────────────────────────────────────────────────


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, OSError, asyncio.CancelledError):
        pass
    finally:
        with contextlib.suppress(Exception):
            if writer.can_write_eof():
                writer.write_eof()


async def _splice(
    a: tuple[asyncio.StreamReader, asyncio.StreamWriter],
    b: tuple[asyncio.StreamReader, asyncio.StreamWriter],
) -> None:
    try:
        await asyncio.gather(_pump(a[0], b[1]), _pump(b[0], a[1]))
    finally:
        for _, writer in (a, b):
            with contextlib.suppress(Exception):
                writer.close()


# ── Win32, through ctypes ─────────────────────────────────────────────

INFINITE = 0xFFFFFFFF
WAIT_OBJECT_0 = 0
STARTF_USESTDHANDLES = 0x100
CREATE_SUSPENDED = 0x4
EXTENDED_STARTUPINFO_PRESENT = 0x80000
HANDLE_FLAG_INHERIT = 0x1
STD_HANDLES = (-10, -11, -12)  # input, output, error
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x20002
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x20009
SYNCHRONIZE = 0x100000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_BASIC_UI_RESTRICTIONS = 4
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
#: No clipboard, no global atoms, no system settings, no logging off,
#: no switching desktops: the extension runs in the background.
UI_RESTRICTIONS = 0x2 | 0x4 | 0x8 | 0x10 | 0x20 | 0x40 | 0x80
PIPE_ACCESS_DUPLEX = 0x3
FILE_FLAG_OVERLAPPED = 0x40000000
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x80000
PIPE_REJECT_REMOTE_CLIENTS = 0x8
PIPE_UNLIMITED_INSTANCES = 255
PIPE_BUFFER = 65536


class SECURITY_ATTRIBUTES(ctypes.Structure):  # noqa: N801 — the Win32 names
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


class SECURITY_CAPABILITIES(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ("AppContainerSid", ctypes.c_void_p),
        ("Capabilities", ctypes.c_void_p),
        ("CapabilityCount", wintypes.DWORD),
        ("Reserved", wintypes.DWORD),
    ]


class STARTUPINFOW(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class STARTUPINFOEXW(ctypes.Structure):  # noqa: N801
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):  # noqa: N801
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_dlls: dict[str, Any] = {}


def _fn(dll: str, name: str, restype: Any, *argtypes: Any) -> Any:
    lib = _dlls.get(dll)
    if lib is None:
        lib = _dlls[dll] = ctypes.WinDLL(dll, use_last_error=True)
    fn = getattr(lib, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


def _check(ok: Any, what: str) -> Any:
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error(), what)
    return ok


def _valid(handle: Any) -> bool:
    return handle not in (None, 0, -1, 2**64 - 1, 2**32 - 1)


def _close(handle: Any) -> None:
    if _valid(handle):
        _fn("kernel32", "CloseHandle", wintypes.BOOL, wintypes.HANDLE)(handle)


class _Sid:
    """A SID from its string form, freed on exit."""

    def __init__(self, text: str) -> None:
        self.pointer = ctypes.c_void_p()
        convert = _fn(
            "advapi32", "ConvertStringSidToSidW", wintypes.BOOL, wintypes.LPCWSTR, ctypes.c_void_p
        )
        _check(convert(text, ctypes.byref(self.pointer)), f"ConvertStringSidToSidW {text}")

    def free(self) -> None:
        _fn("kernel32", "LocalFree", ctypes.c_void_p, ctypes.c_void_p)(self.pointer)


class Job:
    """A job object: its members die when its last handle closes; none may leave."""

    def __init__(self) -> None:
        create = _fn(
            "kernel32", "CreateJobObjectW", wintypes.HANDLE, ctypes.c_void_p, wintypes.LPCWSTR
        )
        self.handle = _check(create(None, None), "CreateJobObjectW")
        set_info = _fn(
            "kernel32",
            "SetInformationJobObject",
            wintypes.BOOL,
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        # Without JOB_OBJECT_LIMIT_BREAKAWAY_OK, no process in the job can
        # start one outside it (CREATE_BREAKAWAY_FROM_JOB fails).
        limits.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
        )
        _check(
            set_info(
                self.handle,
                JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ),
            "SetInformationJobObject (limits)",
        )
        ui = wintypes.DWORD(UI_RESTRICTIONS)
        _check(
            set_info(
                self.handle, JOB_OBJECT_BASIC_UI_RESTRICTIONS, ctypes.byref(ui), ctypes.sizeof(ui)
            ),
            "SetInformationJobObject (ui)",
        )

    def assign(self, process: Any) -> None:
        assign = _fn(
            "kernel32",
            "AssignProcessToJobObject",
            wintypes.BOOL,
            wintypes.HANDLE,
            wintypes.HANDLE,
        )
        _check(assign(self.handle, process), "AssignProcessToJobObject")

    def close(self) -> None:
        """Close it, which kills whatever is still in it."""
        _close(self.handle)
        self.handle = None


def _inheritable_stdio() -> list[Any]:
    get = _fn("kernel32", "GetStdHandle", wintypes.HANDLE, wintypes.DWORD)
    set_info = _fn(
        "kernel32",
        "SetHandleInformation",
        wintypes.BOOL,
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    handles: list[Any] = []
    for which in STD_HANDLES:
        handle = get(which & 0xFFFFFFFF)
        if _valid(handle):
            set_info(handle, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT)
        handles.append(handle)
    return handles


def start_in_container(container_sid: str, argv: list[str], cwd: str | None, job: Job) -> Any:
    """Start ``argv`` in the AppContainer, inside ``job``; return its process handle.

    No capabilities: no internet, no private network, no loopback to
    anything outside the container. It inherits exactly its stdio.
    Started suspended and resumed only once it is in the job, so it
    never runs outside it.
    """
    sid = _Sid(container_sid)
    attributes: Any = None
    try:
        capabilities = SECURITY_CAPABILITIES()
        capabilities.AppContainerSid = sid.pointer
        stdio = _inheritable_stdio()
        inherited = list(dict.fromkeys(h for h in stdio if _valid(h)))

        init = _fn(
            "kernel32",
            "InitializeProcThreadAttributeList",
            wintypes.BOOL,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_size_t),
        )
        update = _fn(
            "kernel32",
            "UpdateProcThreadAttribute",
            wintypes.BOOL,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        size = ctypes.c_size_t()
        init(None, 2, 0, ctypes.byref(size))
        attributes = ctypes.create_string_buffer(size.value)
        _check(init(attributes, 2, 0, ctypes.byref(size)), "InitializeProcThreadAttributeList")
        _check(
            update(
                attributes,
                0,
                PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
                ctypes.byref(capabilities),
                ctypes.sizeof(capabilities),
                None,
                None,
            ),
            "UpdateProcThreadAttribute (security capabilities)",
        )
        handle_array = (wintypes.HANDLE * max(len(inherited), 1))(*inherited)
        if inherited:
            _check(
                update(
                    attributes,
                    0,
                    PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                    handle_array,
                    ctypes.sizeof(wintypes.HANDLE) * len(inherited),
                    None,
                    None,
                ),
                "UpdateProcThreadAttribute (handle list)",
            )

        startup = STARTUPINFOEXW()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        startup.StartupInfo.hStdInput, startup.StartupInfo.hStdOutput = stdio[0], stdio[1]
        startup.StartupInfo.hStdError = stdio[2]
        startup.lpAttributeList = ctypes.cast(attributes, ctypes.c_void_p)
        info = PROCESS_INFORMATION()
        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
        create = _fn(
            "kernel32",
            "CreateProcessW",
            wintypes.BOOL,
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        _check(
            create(
                None,
                command_line,
                None,
                None,
                bool(inherited),
                EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED,
                None,
                cwd,
                ctypes.byref(startup),
                ctypes.byref(info),
            ),
            f"CreateProcessW in AppContainer {container_sid}",
        )
        try:
            job.assign(info.hProcess)
        except OSError:
            _fn("kernel32", "TerminateProcess", wintypes.BOOL, wintypes.HANDLE, wintypes.UINT)(
                info.hProcess, 1
            )
            _close(info.hProcess)
            _close(info.hThread)
            raise
        _fn("kernel32", "ResumeThread", wintypes.DWORD, wintypes.HANDLE)(info.hThread)
        _close(info.hThread)
        return info.hProcess
    finally:
        if attributes is not None:
            _fn("kernel32", "DeleteProcThreadAttributeList", None, ctypes.c_void_p)(attributes)
        sid.free()


def _open_parent(pid: int) -> Any:
    open_process = _fn(
        "kernel32", "OpenProcess", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD
    )
    return open_process(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)


def _wait(process: Any, parent: Any) -> int:
    """Block until the process or igni ends; the process's status, or 1 if igni went."""
    handles = [process] + ([parent] if _valid(parent) else [])
    array = (wintypes.HANDLE * len(handles))(*handles)
    wait = _fn(
        "kernel32",
        "WaitForMultipleObjects",
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
    )
    which = wait(len(handles), array, False, INFINITE)
    if which != WAIT_OBJECT_0:
        return 1
    code = wintypes.DWORD()
    get = _fn(
        "kernel32",
        "GetExitCodeProcess",
        wintypes.BOOL,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    )
    get(process, ctypes.byref(code))
    return int(code.value)


def _secured_pipe_server(sddl: str) -> Any:
    """asyncio's pipe server, its pipes created with ``sddl`` rather than the default.

    asyncio creates every pipe instance with no security attributes,
    which leaves an AppContainer no way in; this class differs only in
    the one method that creates an instance.
    """
    from asyncio import windows_events, windows_utils

    from_string = _fn(
        "advapi32",
        "ConvertStringSecurityDescriptorToSecurityDescriptorW",
        wintypes.BOOL,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    descriptor = ctypes.c_void_p()
    _check(from_string(sddl, 1, ctypes.byref(descriptor), None), "pipe security descriptor")
    attributes = SECURITY_ATTRIBUTES()
    attributes.nLength = ctypes.sizeof(attributes)
    attributes.lpSecurityDescriptor = descriptor
    attributes.bInheritHandle = False
    create = _fn(
        "kernel32",
        "CreateNamedPipeW",
        wintypes.HANDLE,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    )

    base: Any = windows_events.PipeServer

    class SecuredPipeServer(base):
        def _server_pipe_handle(self, first: bool) -> Any:
            if self.closed():
                return None
            flags = PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED
            if first:
                # Nobody else may have made this pipe first and be
                # waiting to read what the extension sends.
                flags |= FILE_FLAG_FIRST_PIPE_INSTANCE
            handle = create(
                self._address,
                flags,
                PIPE_REJECT_REMOTE_CLIENTS,
                PIPE_UNLIMITED_INSTANCES,
                PIPE_BUFFER,
                PIPE_BUFFER,
                0,
                ctypes.byref(attributes),
            )
            if not _valid(handle):
                raise ctypes.WinError(ctypes.get_last_error(), f"CreateNamedPipeW {self._address}")
            pipe = windows_utils.PipeHandle(handle)
            self._free_instances.add(pipe)
            return pipe

    return SecuredPipeServer


# ── The two halves ────────────────────────────────────────────────────


async def _run(options: dict[str, list[str]], command: list[str]) -> int:
    from asyncio import windows_events

    container_sid = options["--sid"][0]
    user_sid = options["--user"][0]
    python = options["--python"][0]
    cwd = options.get("--cwd", [None])[0]
    parent_pid = int(options["--parent"][0])
    forwards = [parse_forward(text) for text in options.get("--forward", [])]
    loop = asyncio.get_running_loop()

    async def relay(port: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            upstream = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            writer.close()
            return
        await _splice((reader, writer), upstream)

    # start_serving_pipe looks PipeServer up in its module when called.
    windows_events.PipeServer = _secured_pipe_server(pipe_sddl(user_sid, container_sid))
    servers: list[Any] = []
    for port, pipe in forwards:

        def factory(port: int = port) -> asyncio.StreamReaderProtocol:
            return asyncio.StreamReaderProtocol(
                asyncio.StreamReader(), lambda r, w: relay(port, r, w)
            )

        servers += await loop.start_serving_pipe(factory, pipe)

    job = Job()
    parent = _open_parent(parent_pid)
    try:
        argv = shim_argv(python, os.path.abspath(__file__), forwards, command)
        process = start_in_container(container_sid, argv, cwd, job)
        try:
            return await loop.run_in_executor(None, _wait, process, parent)
        finally:
            _close(process)
    finally:
        # Closing the job kills what the extension left running.
        job.close()
        _close(parent)
        for server in servers:
            server.close()


def _open_fd(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


async def _shim(options: dict[str, list[str]], command: list[str]) -> int:
    loop = asyncio.get_running_loop()
    forwards = [parse_forward(text) for text in options.get("--forward", [])]

    async def to_pipe(
        pipe: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        inner = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(inner)
        try:
            transport, _ = await loop.create_pipe_connection(lambda: protocol, pipe)
        except OSError:
            writer.close()
            return
        await _splice(
            (reader, writer), (inner, asyncio.StreamWriter(transport, protocol, inner, loop))
        )

    ports: dict[int, int] = {}
    servers = []
    for port, pipe in forwards:
        server = await asyncio.start_server(functools.partial(to_pipe, pipe), "127.0.0.1", 0)
        servers.append(server)
        ports[port] = int(server.sockets[0].getsockname()[1])
    env = rewrite_ports(os.environ, ports)
    # Its stdio passed on explicitly: on Windows a child inherits a pipe
    # handle only when it is redirected, and MCP speaks over these pipes.
    stdio = [fd if _open_fd(fd) else None for fd in (0, 1, 2)]
    child = await asyncio.create_subprocess_exec(
        *command, env=env, stdin=stdio[0], stdout=stdio[1], stderr=stdio[2]
    )
    try:
        return await child.wait()
    finally:
        for server in servers:
            server.close()


def main(argv: list[str]) -> int:
    try:
        mode, options, command = parse_args(argv)
    except ValueError as exc:
        print(f"winbridge.py: {exc}", file=sys.stderr)
        return 2
    if sys.platform != "win32":
        print("winbridge.py: this runs on Windows only", file=sys.stderr)
        return 2
    return asyncio.run(_run(options, command) if mode == "run" else _shim(options, command))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

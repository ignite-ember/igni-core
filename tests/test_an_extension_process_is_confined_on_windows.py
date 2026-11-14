"""On Windows an extension's process runs in an AppContainer of its run's own.

Before, ``launcher._probe`` said "not sandboxed on Windows yet" and the
process ran with the person's full file and network access. Now it
starts in an AppContainer with no capabilities, granted exactly the
plan's paths, inside a job object, reaching igni's proxy and endpoints
through named pipes (:mod:`igni_core.extensions.sandbox.appcontainer`,
:mod:`igni_core.extensions.sandbox.winbridge`).

What does not need Windows — the grants a plan becomes, the names, the
ledger that undoes a run after a crash, the environment the shim
rewrites, the launcher's wiring with the Win32 calls faked — runs
everywhere. The tests that start a real confined process run on Windows
only (the ``sandbox-windows`` job in ``release-readiness.yml``) and fail
there, rather than skip, when the sandbox cannot be set up.

Self-contained: the macOS/Linux sandbox tests import ``pwd``, which
Windows does not have.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from igni_core.core.mcp.config import MCPServerConfig, MCPTransport
from igni_core.core.network_policy import NetworkPolicy
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.sandbox import (
    ExtensionSandbox,
    MechanismDeps,
    MechanismName,
    SandboxPlan,
    SandboxRefused,
    SandboxSettings,
    winbridge,
)
from igni_core.extensions.sandbox.appcontainer import (
    LOW_LABEL_DIR,
    MAX_PROFILE_NAME,
    AppContainer,
    ContainerRun,
    Grant,
    GrantLedger,
    explicit_label,
)

pytestmark = pytest.mark.extension_sandbox

ON_WINDOWS = sys.platform == "win32"


def manifest(**permissions) -> ExtensionManifest:
    return ExtensionManifest.model_validate(
        {
            "id": "acme.probe",
            "version": "1.0.0",
            "contract": ">=1.0 <2",
            "name": "Probe",
            "trust": "unsigned",
            "dev": {"command": ["python", "probe.py"]},
            "permissions": permissions,
        }
    )


def stdio_config(command: str, args: list[str] | None = None, **kw) -> MCPServerConfig:
    return MCPServerConfig(
        name="ext:acme.probe",
        type=MCPTransport.stdio,
        command=command,
        args=args or [],
        env=kw.pop("env", {}),
        cwd=kw.pop("cwd", ""),
        source="extension",
    )


class FakeWindows:
    """The Win32 calls, recorded: what igni would have done to the machine."""

    def __init__(self, *, alive: set[int] | None = None, fail_on: Path | None = None) -> None:
        self.alive = alive or set()
        self.fail_on = fail_on
        self.profiles: dict[str, str] = {}
        self.entries: dict[Path, dict[str, bool]] = {}
        self.labels: dict[Path, str] = {}
        self.calls: list[tuple] = []

    def create_profile(self, name, display, description):
        sid = f"S-1-15-2-{len(self.profiles) + 1}"
        self.profiles[name] = sid
        self.calls.append(("create_profile", name))
        return sid

    def delete_profile(self, name):
        self.profiles.pop(name, None)
        self.calls.append(("delete_profile", name))

    def current_user_sid(self):
        return "S-1-5-21-1-2-3-1001"

    def grant(self, path, sid, *, write):
        if self.fail_on is not None and path == self.fail_on:
            raise PermissionError(5, "Access is denied", str(path))
        self.entries.setdefault(path, {})[sid] = write
        self.calls.append(("grant", path, write))

    def revoke(self, path, sid):
        self.entries.get(path, {}).pop(sid, None)
        self.calls.append(("revoke", path))

    def label(self, path):
        return self.labels.get(path, "")

    def set_label(self, path, sddl):
        self.labels[path] = sddl
        self.calls.append(("set_label", path, sddl))

    def process_alive(self, pid):
        return pid in self.alive

    def granted(self, sid: str) -> dict[Path, bool]:
        return {path: by[sid] for path, by in self.entries.items() if sid in by}


def plan_with(tmp_path: Path, **kw) -> SandboxPlan:
    run = tmp_path / "run"
    (run / "tmp" / "home").mkdir(parents=True, exist_ok=True)
    plan = SandboxPlan(extension_id="acme.probe", run_dir=run)
    plan.read = kw.get("read", [])
    plan.write = kw.get("write", [plan.temp_dir])
    return plan


def tree(*paths: Path) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


# ── The plan: Windows' Python layout ──────────────────────────────────


def test_a_windows_venv_grants_its_environment_and_its_interpreter_and_no_drive(tmp_path):
    """``Scripts\\python.exe`` in a venv, ``python.exe`` in the base prefix.

    The POSIX rule — the directory above the executable's — would have
    granted the base interpreter's parent (every Python uv installed)
    and, for an interpreter at ``C:\\Python312``, the whole drive.
    """
    base = tmp_path / "uv" / "python" / "cpython-3.12"
    venv = tmp_path / "bundle" / ".venv"
    tree(base, venv / "Scripts")
    (base / "python.exe").write_text("")
    (venv / "Scripts" / "python.exe").write_text("")
    (venv / "pyvenv.cfg").write_text(f"home = {base}\nversion = 3.12.4\n")

    paths = SandboxPlan.runtime_paths(str(venv / "Scripts" / "python.exe"), {})
    assert venv in paths and base in paths
    assert base.parent not in paths and tmp_path not in paths
    # The interpreter itself, not in a venv: its own directory.
    assert SandboxPlan.runtime_paths(str(base / "python.exe"), {}) == [base]


def test_a_posix_venv_is_granted_as_before(tmp_path):
    base = tmp_path / "py"
    venv = tmp_path / "venv"
    tree(base / "bin", venv / "bin")
    (base / "bin" / "python3").write_text("")
    (venv / "bin" / "python").write_text("")
    (venv / "pyvenv.cfg").write_text(f"home = {base / 'bin'}\n")
    assert SandboxPlan.runtime_paths(str(venv / "bin" / "python"), {}) == [venv, base]


# ── The grants: exactly the plan's paths ──────────────────────────────


def test_the_grants_are_the_plans_paths_read_and_write_and_no_more(tmp_path):
    bundle, data, venv, ws = (tmp_path / n for n in ("bundle", "data", "venv", "ws"))
    tree(bundle / "assets", data, venv, ws)
    plan = plan_with(tmp_path, read=[bundle, bundle / "assets", venv, ws], write=[data])
    plan.write.append(plan.temp_dir)
    bridge = tmp_path / "run" / "bridge"
    tree(bridge)

    grants = AppContainer.grants(plan, extra_read=[bridge], home=tmp_path / "home", environ={})
    got = {g.path: g.write for g in grants}
    assert got == {
        bundle: False,
        venv: False,
        ws: False,
        data: True,
        bridge: False,
        plan.temp_dir: True,
    }
    # ``assets`` is inside the bundle: the bundle's inherited entry covers it.
    assert bundle / "assets" not in got


def test_a_read_only_workspace_is_granted_read_and_a_write_one_modify(tmp_path):
    ws = tmp_path / "ws"
    tree(ws)
    read = plan_with(tmp_path, read=[ws])
    assert {
        g.path: g.write for g in AppContainer.grants(read, extra_read=[], home=None, environ={})
    }[ws] is False
    write = plan_with(tmp_path, read=[ws], write=[ws])
    assert {
        g.path: g.write for g in AppContainer.grants(write, extra_read=[], home=None, environ={})
    }[ws] is True


def test_a_drive_root_or_the_persons_home_is_never_granted(tmp_path, caplog):
    home = tmp_path / "Users" / "someone"
    tree(home / "project")
    root = Path(tmp_path.anchor)
    plan = plan_with(tmp_path, read=[root, tmp_path / "Users", home, home / "project"])
    got = [g.path for g in AppContainer.grants(plan, extra_read=[], home=home, environ={})]
    assert home / "project" in got
    assert root not in got and home not in got and tmp_path / "Users" not in got
    assert "too broad" in caplog.text


def test_what_every_appcontainer_reads_already_is_not_granted_again(tmp_path):
    windows = tmp_path / "Windows"
    program_files = tmp_path / "Program Files"
    tree(windows / "System32", program_files / "Python312")
    plan = plan_with(tmp_path, read=[windows / "System32", program_files / "Python312"])
    got = AppContainer.grants(
        plan,
        extra_read=[],
        home=None,
        environ={"SystemRoot": str(windows), "ProgramFiles": str(program_files)},
    )
    assert [g.path for g in got] == [plan.temp_dir]


def test_a_path_that_does_not_exist_has_nothing_to_carry_an_entry(tmp_path):
    plan = plan_with(tmp_path, read=[tmp_path / "missing"])
    assert [g.path for g in AppContainer.grants(plan, extra_read=[], home=None, environ={})] == [
        plan.temp_dir
    ]


# ── Names ─────────────────────────────────────────────────────────────


def test_a_profile_name_is_the_extension_and_the_run_within_windows_limit():
    name = AppContainer.profile_name("acme.probe", "0123456789ab")
    assert name == "igni.acme.probe.0123456789ab"
    long = AppContainer.profile_name("x" * 200 + "/weird id", "0123456789ab")
    assert len(long) <= MAX_PROFILE_NAME
    assert re.fullmatch(r"[A-Za-z0-9.-]+", long)
    assert AppContainer.profile_name("a", "1") != AppContainer.profile_name("a", "2")


def test_a_pipe_is_named_for_its_run_and_port():
    assert AppContainer.pipe_name("igni.acme.probe.ab", 41001) == (
        "\\\\.\\pipe\\igni.acme.probe.ab-41001"
    )


def test_only_a_labels_own_entries_are_put_back():
    assert explicit_label("") == "S:"
    assert explicit_label("S:AI(ML;ID;NW;;;LW)") == "S:"
    assert explicit_label("S:(ML;OICI;NW;;;ME)") == "S:(ML;OICI;NW;;;ME)"
    assert explicit_label("S:(ML;OICIID;NW;;;LW)(ML;;NW;;;HI)") == "S:(ML;;NW;;;HI)"


# ── A run: granted, recorded, undone ──────────────────────────────────


def started(tmp_path: Path, calls: FakeWindows, **kw):
    data = tmp_path / "data"
    tree(data)
    plan = plan_with(tmp_path, read=[tmp_path / "bundle"], write=[data])
    plan.write.append(plan.temp_dir)
    tree(tmp_path / "bundle")
    container = AppContainer(calls=calls, ledger_dir=tmp_path / "ledger")
    run, command, args = container.start(
        plan,
        "python",
        ["server.py"],
        cwd=tmp_path / "bundle",
        forwards=[3128, 41001],
        home=tmp_path / "home",
        **kw,
    )
    return container, plan, run, command, args


def test_a_run_is_recorded_before_anything_is_granted_and_forgotten_once_undone(tmp_path):
    calls = FakeWindows()
    container, plan, run, command, args = started(tmp_path, calls)
    granted = calls.granted(run.sid)
    assert granted[tmp_path / "data"] is True and granted[plan.temp_dir] is True
    assert granted[tmp_path / "bundle"] is False
    assert granted[AppContainer.bridge_dir(plan)] is False
    # Written: an AppContainer runs at low integrity and cannot write up.
    assert calls.labels[tmp_path / "data"] == LOW_LABEL_DIR
    assert calls.labels.get(tmp_path / "bundle") is None
    # The ledger says what to undo.
    [entry] = container.ledger.entries()
    assert entry.sid == run.sid and entry.owner == os.getpid()
    assert {g.path for g in entry.grants} == set(granted)

    container.stop(run)
    assert calls.granted(run.sid) == {}
    assert calls.labels[tmp_path / "data"] == "S:"  # the label it had: none
    assert run.profile not in calls.profiles
    assert container.ledger.entries() == []


def test_the_runner_is_told_the_container_the_pipes_and_the_command(tmp_path):
    calls = FakeWindows()
    _, plan, run, command, args = started(tmp_path, calls)
    assert command == sys.executable
    bridge = AppContainer.bridge_dir(plan) / "winbridge.py"
    assert args[:3] == ["-I", str(bridge), "run"] and bridge.is_file()
    joined = " ".join(args)
    assert f"--sid {run.sid}" in joined
    assert f"--parent {os.getpid()}" in joined
    assert f"--forward 3128={AppContainer.pipe_name(run.profile, 3128)}" in joined
    assert f"--forward 41001={AppContainer.pipe_name(run.profile, 41001)}" in joined
    assert args[-3:] == ["--", "python", "server.py"]
    # igni's own interpreter runs the shim inside, so it is readable there.
    assert any(
        Path(sys.executable).resolve().is_relative_to(path) for path in calls.granted(run.sid)
    )


def test_a_write_grant_that_fails_undoes_the_run_and_says_so(tmp_path):
    calls = FakeWindows(fail_on=tmp_path / "data")
    with pytest.raises(PermissionError):
        started(tmp_path, calls)
    assert all(not by for by in calls.entries.values())
    assert calls.profiles == {}
    assert GrantLedger(tmp_path / "ledger").entries() == []


def test_a_read_grant_that_fails_is_left_to_what_every_appcontainer_reads(tmp_path, caplog):
    calls = FakeWindows(fail_on=tmp_path / "bundle")
    container, _, run, _, _ = started(tmp_path, calls)
    assert tmp_path / "bundle" not in calls.granted(run.sid)
    assert "could not grant its sandbox read access" in caplog.text
    [entry] = container.ledger.entries()
    assert tmp_path / "bundle" not in {g.path for g in entry.grants}


def test_nothing_is_granted_before_the_ledger_says_so(tmp_path):
    ledger = tmp_path / "ledger"

    class Checking(FakeWindows):
        def grant(self, path, sid, *, write):
            assert any(sid in p.read_text() for p in ledger.glob("*.json")), "granted unrecorded"
            super().grant(path, sid, write=write)

    started(tmp_path, Checking())


def test_a_label_that_cannot_be_lowered_is_a_warning_and_nothing_to_restore(tmp_path, caplog):
    class NoLabels(FakeWindows):
        def set_label(self, path, sddl):
            raise PermissionError(5, "Access is denied", str(path))

    calls = NoLabels()
    container, _, run, _, _ = started(tmp_path, calls)
    assert calls.granted(run.sid)[tmp_path / "data"] is True
    assert "could not lower the integrity label" in caplog.text
    assert all(g.label is None for g in run.grants)
    container.stop(run)
    assert container.ledger.entries() == []


def test_a_run_left_by_an_igni_that_crashed_is_undone_on_the_next_start(tmp_path):
    calls = FakeWindows()
    ledger = GrantLedger(tmp_path / "ledger")
    data = tmp_path / "data"
    left_dir = tmp_path / "igni-ext-acme-left"
    tree(data, left_dir)
    calls.entries[data] = {"S-1-15-2-99": True}
    calls.labels[data] = LOW_LABEL_DIR
    calls.profiles["igni.acme.probe.dead"] = "S-1-15-2-99"
    ledger.record(
        ContainerRun(
            profile="igni.acme.probe.dead",
            sid="S-1-15-2-99",
            owner=999_999,
            run_dir=str(left_dir),
            grants=[Grant(path=data, write=True, label="")],
        )
    )
    # Another igni still running: its run is not a leftover.
    ledger.record(ContainerRun(profile="igni.other.live", sid="S-1-15-2-7", owner=4242))
    calls.alive = {4242}

    assert AppContainer(calls=calls, ledger_dir=ledger.directory).sweep() == 1
    assert calls.granted("S-1-15-2-99") == {}
    assert calls.labels[data] == "S:"
    assert "igni.acme.probe.dead" not in calls.profiles
    assert not left_dir.exists()
    assert [e.profile for e in ledger.entries()] == ["igni.other.live"]


def test_a_run_whose_undo_fails_stays_in_the_ledger(tmp_path):
    class Stuck(FakeWindows):
        def revoke(self, path, sid):
            raise PermissionError(5, "Access is denied", str(path))

    calls = Stuck()
    container, _, run, _, _ = started(tmp_path, calls)
    container.stop(run)
    assert [e.profile for e in container.ledger.entries()] == [run.profile]


# ── The launcher on Windows, with the Win32 calls faked ───────────────


@pytest.fixture
def windows_here(monkeypatch):
    async def usable():
        return None

    monkeypatch.setattr(AppContainer, "probe", staticmethod(usable))


async def test_the_launcher_confines_a_windows_process_and_undoes_it_on_release(
    tmp_path, windows_here
):
    calls = FakeWindows()
    sandbox = ExtensionSandbox(
        settings=SandboxSettings(),
        network=NetworkPolicy(),
        platform="win32",
        run_root=tmp_path,
        deps=MechanismDeps(appcontainer=AppContainer(calls=calls, ledger_dir=tmp_path / "ledger")),
    )
    bundle, data = tmp_path / "bundle", tmp_path / "data"
    tree(bundle, data)
    config = stdio_config(
        sys.executable,
        ["probe.py"],
        env={"IGNI_HOST_URL": "ws://127.0.0.1:41001/v1/host"},
        cwd=str(bundle),
    )
    try:
        wrapped = await sandbox.confine(
            manifest(egress=["api.acme.com"]),
            config,
            bundle=bundle,
            data_dir=data,
            project_dir=None,
            service_url=None,
            development=False,
        )
        status = sandbox.status("acme.probe")
        assert status is not None and status.describe() == "sandboxed (appcontainer)"
        assert status.container is not None
        sid = status.container.sid
        proxy_port = status.proxy.port
        env = wrapped.env or {}
        assert env["IGNI_SANDBOX"] == "appcontainer"
        assert env["HTTPS_PROXY"].endswith(f"@127.0.0.1:{proxy_port}")
        assert env["USERPROFILE"] == str(status.plan.home_dir)
        assert env["APPDATA"].startswith(str(status.plan.home_dir))
        assert env["TEMP"] == str(status.plan.temp_dir)
        args = wrapped.args or []
        assert wrapped.command == sys.executable and "run" in args
        profile = status.container.profile
        for port in (proxy_port, 41001):
            assert f"{port}={AppContainer.pipe_name(profile, port)}" in args
        assert calls.granted(sid)[data.resolve()] is True
        run_dir = status.plan.run_dir
    finally:
        await sandbox.close()
    assert calls.granted(sid) == {}
    assert profile not in calls.profiles
    assert GrantLedger(tmp_path / "ledger").entries() == []
    assert not run_dir.exists()


async def test_on_a_windows_without_appcontainers_require_sandbox_refuses(tmp_path, monkeypatch):
    async def too_old():
        return "Windows build 9600 is older than Windows 10, which the sandbox needs"

    monkeypatch.setattr(AppContainer, "probe", staticmethod(too_old))
    sandbox = ExtensionSandbox(settings=SandboxSettings(required=True), platform="win32")
    with pytest.raises(SandboxRefused, match="require_sandbox.*older than Windows 10"):
        await sandbox.confine(
            manifest(),
            stdio_config(sys.executable),
            bundle=tmp_path,
            data_dir=None,
            project_dir=None,
            service_url=None,
            development=False,
        )


async def test_off_windows_the_appcontainer_probe_says_why_not():
    if ON_WINDOWS:
        pytest.skip("this is Windows")
    assert await AppContainer.probe() == "Windows AppContainers exist only on Windows"


# ── The bridge: arguments, the pipe's security, the environment ───────


def test_the_shim_rewrites_igni_s_loopback_urls_to_its_own_ports():
    env = {
        "HTTPS_PROXY": "http://igni:tok@127.0.0.1:3128",
        "IGNI_HOST_URL": "ws://127.0.0.1:41001/v1/host",
        "IGNI_SERVICE_TOKEN_URL": "http://localhost:41002/token",
        "OTHER": "http://127.0.0.1:410011/x and https://api.acme.com:41001",
        "NO_PROXY": "localhost,127.0.0.1,::1",
    }
    out = winbridge.rewrite_ports(env, {3128: 50000, 41001: 50001, 41002: 50002})
    assert out["HTTPS_PROXY"] == "http://igni:tok@127.0.0.1:50000"
    assert out["IGNI_HOST_URL"] == "ws://127.0.0.1:50001/v1/host"
    assert out["IGNI_SERVICE_TOKEN_URL"] == "http://127.0.0.1:50002/token"
    # Another port that starts with the same digits, and another host: untouched.
    assert out["OTHER"] == env["OTHER"]
    assert out["NO_PROXY"] == env["NO_PROXY"]


def test_the_bridge_reads_its_arguments_and_refuses_what_it_does_not_know():
    mode, options, command = winbridge.parse_args(
        ["run", "--sid", "S-1-15-2-1", "--forward", "3128=\\\\.\\pipe\\x-3128", "--", "py", "-V"]
    )
    assert mode == "run" and command == ["py", "-V"]
    assert winbridge.parse_forward(options["--forward"][0]) == (3128, "\\\\.\\pipe\\x-3128")
    for bad in (["run", "--", "x"][:2], ["fly", "--", "x"], ["run", "--sid", "--", "x"]):
        with pytest.raises(ValueError):
            winbridge.parse_args(bad)
    with pytest.raises(ValueError):
        winbridge.parse_forward("3128=/tmp/not-a-pipe")


def test_the_shim_puts_back_where_the_run_keeps_its_files():
    """Windows points TEMP, TMP and LOCALAPPDATA of a process it starts in
    an AppContainer at the container's own folder; the runner hands igni's
    values to the shim, which puts them back."""
    told = {
        "TEMP": "C:\\run\\tmp",
        "LOCALAPPDATA": "C:\\run\\tmp\\home\\AppData\\Local",
        "PATH": "x",
    }
    kept = winbridge.kept_env(told)
    assert kept == {"TEMP": "C:\\run\\tmp", "LOCALAPPDATA": told["LOCALAPPDATA"]}
    argv = winbridge.shim_argv("py", "wb.py", [], ["x"], kept)
    _, options, _ = winbridge.parse_args(argv[3:])
    substituted = {"TEMP": "C:\\Users\\p\\AppData\\Local\\Packages\\igni.x\\AC\\Temp", "PATH": "y"}
    restored = winbridge.restore_env(substituted, options["--setenv"])
    assert restored["TEMP"] == "C:\\run\\tmp"
    assert restored["LOCALAPPDATA"] == told["LOCALAPPDATA"]
    assert restored["PATH"] == "y"  # only the run's file locations
    with pytest.raises(ValueError):
        winbridge.restore_env({}, ["PATH=C:\\evil"])


def test_the_shim_runs_igni_s_python_isolated_with_the_same_pipes():
    argv = winbridge.shim_argv(
        "C:\\py\\python.exe",
        "C:\\run\\bridge\\winbridge.py",
        [(1, "\\\\.\\pipe\\p-1")],
        ["node", "x.js"],
    )
    assert argv == [
        "C:\\py\\python.exe",
        "-I",
        "C:\\run\\bridge\\winbridge.py",
        "shim",
        "--forward",
        "1=\\\\.\\pipe\\p-1",
        "--",
        "node",
        "x.js",
    ]


def test_a_pipe_admits_the_person_and_the_container_only_at_low_integrity():
    sddl = winbridge.pipe_sddl("S-1-5-21-1-2-3-1001", "S-1-15-2-9")
    assert sddl.startswith("D:P")  # nothing inherited
    assert "(A;;GRGW;;;S-1-15-2-9)" in sddl
    assert "(A;;GA;;;S-1-5-21-1-2-3-1001)" in sddl
    assert "AC)" not in sddl and ";WD)" not in sddl  # not every container, not everyone
    assert sddl.endswith("S:(ML;;NW;;;LW)")


def test_the_bridge_says_it_needs_windows_elsewhere(capsys):
    if ON_WINDOWS:
        pytest.skip("this is Windows")
    assert winbridge.main(["shim", "--", "true"]) == 2
    assert "Windows only" in capsys.readouterr().err


# ── Windows: the real AppContainer ────────────────────────────────────


class Upstream:
    """An HTTP server on loopback; the proxy's connector maps every name to it."""

    def __init__(self) -> None:
        self.port = 0
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._answer, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _answer(self, reader, writer) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        host = next(
            (
                line.split(b":", 1)[1].strip()
                for line in head.split(b"\r\n")
                if line.lower().startswith(b"host:")
            ),
            b"?",
        )
        body = b"hello from " + host
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s"
            % (len(body), body)
        )
        await writer.drain()
        writer.close()

    async def connect(self, host: str, port: int):
        return await asyncio.open_connection("127.0.0.1", self.port)


@pytest.fixture
async def upstream():
    server = Upstream()
    await server.start()
    yield server
    await server.stop()


#: Run inside the sandbox; prints what it could and could not do.
PROBE = r"""
import json, os, socket, subprocess, sys, time, urllib.error, urllib.request
from pathlib import Path
from urllib.parse import urlsplit

out = {}
person, outside, workspace, mode = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]

def attempt(name, fn):
    try:
        out[name] = ["ok", fn()]
    except urllib.error.HTTPError as exc:
        out[name] = ["http", exc.code]
    except OSError as exc:
        out[name] = ["oserror", exc.errno]

def get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.read().decode()

def direct(host, port):
    s = socket.create_connection((host, port), timeout=5)
    try:
        return s.recv(16).decode() or "connected"
    finally:
        s.close()

if mode == "hold":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    print(json.dumps({"process": os.getpid(), "grandchild": child.pid}), flush=True)
    time.sleep(600)

attempt("allowed_via_proxy", lambda: get("http://allowed.test:8080/hello"))
attempt("refused_via_proxy", lambda: get("http://evil.test:8080/"))
attempt("direct_public_ip", lambda: direct("1.1.1.1", 80))
attempt("dns", lambda: socket.getaddrinfo("example.com", 443) and "resolved")
host = urlsplit(os.environ["IGNI_HOST_URL"])
attempt("host_channel_as_told", lambda: direct(host.hostname, host.port))
attempt("host_channel_direct", lambda: direct("127.0.0.1", int(os.environ["PROBE_HOST_CHANNEL_PORT"])))
attempt("igni_listener_direct", lambda: direct("127.0.0.1", int(os.environ["PROBE_IGNI_LISTENER_PORT"])))

name = f"igni-sandbox-probe-{os.getpid()}"
attempt("read_bundle", lambda: Path(__file__).read_text()[:6])
attempt("read_outside", lambda: (outside / "secret.txt").read_text())
attempt("list_person", lambda: len(os.listdir(person)))
attempt("write_person", lambda: (person / name).write_text("x") and "written")
attempt("write_outside", lambda: (outside / name).write_text("x") and "written")
attempt("write_workspace", lambda: (workspace / name).write_text("x") and "written")
attempt("write_data", lambda: (Path(os.environ["IGNI_EXTENSION_DATA"]) / name).write_text("x") and "written")
attempt("write_tmp", lambda: (Path(os.environ["TEMP"]) / name).write_text("x") and "written")
out["env"] = {k: os.environ.get(k, "") for k in ("IGNI_SANDBOX", "HTTP_PROXY", "USERPROFILE", "IGNI_HOST_URL", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA")}
print(json.dumps(out), flush=True)
"""


def alive(pid: int) -> bool:
    from igni_core.extensions.sandbox import winapi

    return winapi.process_alive(pid)


def wait_dead(pid: int, seconds: float = 15) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.2)
    return False


class Rig:
    """igni's side of a run: the sandbox, a host channel, another igni listener."""

    def __init__(self, tmp_path: Path, upstream: Upstream) -> None:
        self.tmp = tmp_path
        self.sandbox = ExtensionSandbox(
            settings=SandboxSettings(),
            network=NetworkPolicy(),
            connect=upstream.connect,
            deps=MechanismDeps(appcontainer=AppContainer(ledger_dir=tmp_path / "ledger")),
        )
        # The person's real profile: on Windows ``~`` is USERPROFILE,
        # which the suite leaves alone (it redirects HOME and Path.home).
        self.person = Path(os.path.expanduser("~"))
        self.outside = tmp_path / "outside"
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text("not for the extension")

    async def __aenter__(self) -> Rig:
        availability = await self.sandbox.availability()
        if availability.name != MechanismName.APPCONTAINER:
            pytest.fail(f"the Windows sandbox could not be set up here: {availability.reason}")

        async def greet(reader, writer):
            writer.write(b"igni")
            await writer.drain()
            writer.close()

        self.host_channel = await asyncio.start_server(greet, "127.0.0.1", 0)
        self.igni_listener = await asyncio.start_server(greet, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *_) -> None:
        self.host_channel.close()
        self.igni_listener.close()
        await self.sandbox.close()

    async def confine(self, m: ExtensionManifest, mode: str) -> tuple[MCPServerConfig, dict]:
        bundle, ws, data = (self.tmp / n for n in ("bundle", "ws", "data"))
        for path in (bundle, ws, data):
            path.mkdir(exist_ok=True)
        (bundle / "probe.py").write_text(PROBE)
        host_port = self.host_channel.sockets[0].getsockname()[1]
        igni_port = self.igni_listener.sockets[0].getsockname()[1]
        config = stdio_config(
            sys.executable,
            [str(bundle / "probe.py"), str(self.person), str(self.outside), str(ws), mode],
            env={
                "IGNI_EXTENSION_DATA": str(data),
                "IGNI_HOST_URL": f"ws://127.0.0.1:{host_port}/v1/host",
                "PROBE_HOST_CHANNEL_PORT": str(host_port),
                "PROBE_IGNI_LISTENER_PORT": str(igni_port),
            },
            cwd=str(bundle),
        )
        wrapped = await self.sandbox.confine(
            m,
            config,
            bundle=bundle,
            data_dir=data,
            project_dir=ws,
            service_url=None,
            development=False,
        )
        return wrapped, {**os.environ, **(wrapped.env or {})}

    async def run(self, m: ExtensionManifest) -> dict:
        wrapped, env = await self.confine(m, "probe")
        #: What igni told the process, to compare with what it saw.
        self.told = dict(wrapped.env or {})
        proc = await asyncio.create_subprocess_exec(
            wrapped.command,
            *(wrapped.args or []),
            cwd=wrapped.cwd or None,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), 120)
        assert proc.returncode == 0, err.decode(errors="replace")
        return json.loads(out.decode().strip().splitlines()[-1])


@pytest.mark.skipif(not ON_WINDOWS, reason="AppContainer is Windows")
async def test_on_windows_the_process_is_held_to_its_grants(tmp_path, upstream):
    async with Rig(tmp_path, upstream) as rig:
        seen = await rig.run(manifest(workspace="write", egress=["allowed.test:8080"]))
        sandbox_status = rig.sandbox.status("acme.probe")
        assert sandbox_status is not None and sandbox_status.mechanism == "appcontainer"
        told = rig.told
    # Network: the declared host through the proxy, nothing directly.
    assert seen["allowed_via_proxy"] == ["ok", "hello from allowed.test:8080"]
    assert seen["refused_via_proxy"] == ["http", 403]
    assert seen["direct_public_ip"][0] == "oserror"
    # igni's endpoints: through the URL it was told, and not otherwise.
    assert seen["host_channel_as_told"] == ["ok", "igni"]
    assert seen["host_channel_direct"][0] == "oserror"
    assert seen["igni_listener_direct"][0] == "oserror"
    # Files: its bundle, its data, its temp, its workspace grant — nothing else.
    assert seen["read_bundle"][0] == "ok"
    assert seen["read_outside"][0] == "oserror"
    assert seen["list_person"][0] == "oserror"
    assert seen["write_person"][0] == "oserror"
    assert seen["write_outside"][0] == "oserror"
    assert seen["write_workspace"] == ["ok", "written"]
    assert seen["write_data"] == ["ok", "written"]
    # Its temp and app data are the run's, as igni set them — not the
    # container folder Windows substitutes for a process it starts there.
    for name in ("TEMP", "TMP", "APPDATA", "LOCALAPPDATA", "USERPROFILE"):
        assert seen["env"][name] == told[name], (name, seen["env"])
    assert seen["write_tmp"] == ["ok", "written"], seen["env"]
    assert seen["env"]["IGNI_SANDBOX"] == "appcontainer"
    assert seen["env"]["USERPROFILE"] != os.path.expanduser("~")
    # Reported, not asserted: whether the system resolver answers a
    # container with no network capability is Windows' decision.
    print("dns inside the AppContainer:", seen["dns"])


@pytest.mark.skipif(not ON_WINDOWS, reason="AppContainer is Windows")
async def test_on_windows_a_read_only_workspace_cannot_be_written(tmp_path, upstream):
    async with Rig(tmp_path, upstream) as rig:
        seen = await rig.run(manifest(workspace="read"))
    assert seen["write_workspace"][0] == "oserror"
    # No egress declared: nothing is reachable through the proxy either.
    assert seen["allowed_via_proxy"] == ["http", 403]


@pytest.mark.skipif(not ON_WINDOWS, reason="AppContainer is Windows")
async def test_on_windows_the_process_tree_dies_with_the_run(tmp_path, upstream):
    """Killing the runner — what igni's MCP client does to stop it — takes
    the extension and every process it started with it."""
    async with Rig(tmp_path, upstream) as rig:
        wrapped, env = await rig.confine(manifest(), "hold")
        proc = subprocess.Popen(
            [wrapped.command, *(wrapped.args or [])],
            cwd=wrapped.cwd or None,
            env=env,
            stdout=subprocess.PIPE,
        )
        try:
            line = await asyncio.wait_for(asyncio.to_thread(proc.stdout.readline), 60)
            pids = json.loads(line)
            assert alive(pids["process"]) and alive(pids["grandchild"])
        finally:
            proc.kill()
            proc.wait(30)
        assert wait_dead(pids["process"])
        assert wait_dead(pids["grandchild"])


@pytest.mark.skipif(not ON_WINDOWS, reason="AppContainer is Windows")
async def test_on_windows_release_takes_every_grant_back(tmp_path, upstream):
    async with Rig(tmp_path, upstream) as rig:
        await rig.run(manifest(workspace="write"))
        status = rig.sandbox.status("acme.probe")
        assert status is not None and status.container is not None
        sid, profile = status.container.sid, status.container.profile
        granted = [g.path for g in status.container.grants]
        assert granted
    for path in granted:
        if path.exists():
            acl = subprocess.run(["icacls", str(path)], capture_output=True, text=True).stdout
            assert sid not in acl and profile not in acl, acl
    assert GrantLedger(tmp_path / "ledger").entries() == []


SERVER = r"""
import os
from mcp.server.mcpserver import MCPServer
server = MCPServer("probe")

@server.tool()
def where() -> str:
    "Which sandbox this runs in."
    return os.environ.get("IGNI_SANDBOX", "none")

server.run()
"""


@pytest.mark.skipif(not ON_WINDOWS, reason="AppContainer is Windows")
async def test_on_windows_an_installed_extension_speaks_mcp_from_inside(tmp_path):
    from igni_core.core.mcp.client import MCPClientManager
    from igni_core.extensions.installer import ExtensionInstaller
    from igni_core.extensions.registry import ExtensionRegistry, ExtensionState
    from igni_core.extensions.rpc import ExtensionRpc

    sandbox = ExtensionSandbox(
        settings=SandboxSettings(),
        deps=MechanismDeps(appcontainer=AppContainer(ledger_dir=tmp_path / "ledger")),
    )
    availability = await sandbox.availability()
    if availability.name != MechanismName.APPCONTAINER:
        pytest.fail(f"the Windows sandbox could not be set up here: {availability.reason}")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "server.py").write_text(SERVER)
    raw = json.loads(manifest().model_dump_json())
    raw["dev"] = {"command": [sys.executable, "server.py"]}
    raw["permissions"]["rpc"] = ["ext.acme.probe.*"]
    raw["permissions"]["tools"] = [
        {
            "name": "acme.probe.where",
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        }
    ]
    (bundle / "manifest.json").write_text(json.dumps(raw))
    registry = ExtensionRegistry()
    rpc = ExtensionRpc(registry)
    mcp = MCPClientManager(project_dir=tmp_path)
    installer = ExtensionInstaller(
        registry=registry, rpc=rpc, mcp=mcp, allow_development=True, sandbox=sandbox
    )
    try:
        entry = await installer.install(bundle)
        assert entry.state is ExtensionState.READY, entry.reason
        assert entry.sandbox == "sandboxed (appcontainer)"
        answer = await rpc.dispatch(caller="acme.probe", method="ext.acme.probe.where", args={})
        assert "appcontainer" in json.dumps(answer)
    finally:
        await installer.uninstall("acme.probe")
        await mcp.disconnect_all()
    assert sandbox.status("acme.probe") is None
    assert GrantLedger(tmp_path / "ledger").entries() == []

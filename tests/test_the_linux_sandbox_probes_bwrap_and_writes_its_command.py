"""The Linux sandbox asks ``bwrap`` whether it can run, and writes its command line.

:class:`~igni_core.extensions.sandbox.bubblewrap.Bubblewrap` is checked
here on any OS: the probe runs a stand-in ``bwrap`` script found on
``PATH`` (the real one runs in
``test_an_extension_process_is_confined_to_its_grants.py``, on Linux with
bubblewrap), and the command line is only text, so it is read as such.
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
from pathlib import Path

import pytest

from igni_core.extensions.sandbox import bubblewrap
from igni_core.extensions.sandbox.bubblewrap import PROXY_PORT, Bubblewrap
from igni_core.extensions.sandbox.plan import SandboxPlan

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the stand-in bwrap is a shell script"
)


def _stand_in(tmp_path: Path, monkeypatch, body: str) -> Path:
    """A ``bwrap`` on ``PATH`` that runs ``body`` (shell)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "bwrap"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return script


# ── executable and probe ──────────────────────────────────────────────


def test_the_executable_is_bwrap_on_the_path_or_nothing(tmp_path, monkeypatch):
    script = _stand_in(tmp_path, monkeypatch, "exit 0")
    assert Bubblewrap.executable() == str(script)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert Bubblewrap.executable() is None


async def test_without_bwrap_the_probe_says_it_is_not_installed(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert await Bubblewrap.probe() == "bubblewrap (bwrap) is not installed"


async def test_a_bwrap_that_makes_a_network_namespace_is_usable(tmp_path, monkeypatch):
    seen = tmp_path / "seen"
    _stand_in(tmp_path, monkeypatch, f'echo "$@" > {seen}')
    assert await Bubblewrap.probe() is None
    told = seen.read_text().split()
    assert told[:7] == ["--unshare-net", "--ro-bind", "/", "/", "--dev", "/dev", "--proc"]
    assert told[7] == "/proc"


async def test_a_bwrap_that_refuses_gives_its_own_words_as_the_reason(tmp_path, monkeypatch):
    _stand_in(
        tmp_path,
        monkeypatch,
        "printf 'bwrap: No permissions to create new namespace\\n\\xff\\n' >&2; exit 1",
    )
    reason = await Bubblewrap.probe()
    assert reason is not None
    assert reason.startswith(
        "bwrap cannot create a sandbox here: bwrap: No permissions to create new namespace"
    )


async def test_a_bwrap_that_cannot_be_started_is_a_reason_not_a_crash(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "bwrap").write_text("not executable\n")
    (bin_dir / "bwrap").chmod(0o755)  # which() finds it; starting it fails: no interpreter line
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    reason = await Bubblewrap.probe()
    assert reason is not None and reason.startswith("bwrap could not run: ")


async def test_a_bwrap_that_hangs_is_reported_and_not_left_running(tmp_path, monkeypatch):
    pid_file = tmp_path / "pid"
    _stand_in(tmp_path, monkeypatch, f"echo $$ > {pid_file}\nexec sleep 60")
    real_wait_for = asyncio.wait_for

    async def quickly(awaitable, timeout):
        assert timeout == 10  # what the probe allows; the test only does not wait for it
        return await real_wait_for(awaitable, 0.5)

    monkeypatch.setattr(bubblewrap.asyncio, "wait_for", quickly)

    reason = await Bubblewrap.probe()

    assert reason == "bwrap could not run: it did not answer within 10 seconds"
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


# ── the command line ──────────────────────────────────────────────────


def _plan(tmp_path: Path, *, read=(), write=(), ports=()) -> SandboxPlan:
    run = tmp_path / "run"
    plan = SandboxPlan(extension_id="acme.probe", run_dir=run)
    plan.read = list(read)
    plan.write = list(write)
    plan.loopback_ports = list(ports)
    Bubblewrap.socket_dir(plan).mkdir(parents=True, exist_ok=True)
    return plan


def _command(plan: SandboxPlan, **kw) -> list[str]:
    kw.setdefault("cwd", None)
    kw.setdefault("home", Path("/home/someone"))
    _, argv = Bubblewrap.command(plan, "node", ["x.js"], **kw)
    return argv


def _mounts(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


def test_the_names_of_the_sockets_come_from_the_run_directory(tmp_path):
    plan = _plan(tmp_path)
    assert Bubblewrap.socket_dir(plan) == tmp_path / "run" / "net"
    assert Bubblewrap.proxy_socket(plan) == tmp_path / "run" / "net" / "proxy.sock"
    assert Bubblewrap.relay_socket(plan, 41001) == tmp_path / "run" / "net" / "port-41001.sock"


def test_the_command_is_the_found_bwrap_and_falls_back_to_the_name(tmp_path, monkeypatch):
    plan = _plan(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert Bubblewrap.command(plan, "node", [], cwd=None)[0] == "bwrap"
    script = _stand_in(tmp_path, monkeypatch, "exit 0")
    assert Bubblewrap.command(plan, "node", [], cwd=None)[0] == str(script)


def test_the_process_gets_namespaces_of_its_own_and_dies_with_igni(tmp_path):
    argv = _command(_plan(tmp_path))
    for flag in ("--die-with-parent", "--unshare-net", "--unshare-ipc", "--unshare-uts"):
        assert flag in argv
    assert "--unshare-cgroup-try" in argv
    assert argv[argv.index("--ro-bind") : argv.index("--ro-bind") + 3] == ["--ro-bind", "/", "/"]
    assert "--unshare-pid" not in argv  # the process group igni checks stays bwrap's
    assert "--new-session" not in argv


def test_the_bridge_is_copied_beside_the_sockets_and_run_with_the_command(tmp_path):
    plan = _plan(tmp_path)
    argv = _command(plan, bridge_python="/opt/py/bin/python")
    copy = Bubblewrap.socket_dir(plan) / "bridge.py"
    assert copy.read_bytes() == Path(bubblewrap.__file__).with_name("bridge.py").read_bytes()
    assert argv[argv.index("--") + 1 :] == [
        "/opt/py/bin/python",
        "-I",
        str(copy),
        "--forward",
        f"{PROXY_PORT}={Bubblewrap.proxy_socket(plan)}",
        "--",
        "node",
        "x.js",
    ]


def test_without_a_python_given_the_bridge_runs_under_igni_s(tmp_path):
    argv = _command(_plan(tmp_path))
    assert argv[argv.index("--") + 1] == sys.executable


def test_each_loopback_port_is_forwarded_to_its_own_socket_after_the_proxy(tmp_path):
    plan = _plan(tmp_path, ports=[41001, 41002])
    argv = _command(plan)
    forwards = [argv[i + 1] for i, a in enumerate(argv) if a == "--forward"]
    assert forwards == [
        f"{PROXY_PORT}={Bubblewrap.proxy_socket(plan)}",
        f"41001={Bubblewrap.relay_socket(plan, 41001)}",
        f"41002={Bubblewrap.relay_socket(plan, 41002)}",
    ]


def test_a_working_directory_is_set_only_when_given(tmp_path):
    assert "--chdir" not in _command(_plan(tmp_path))
    argv = _command(_plan(tmp_path), cwd=tmp_path / "bundle")
    assert argv[argv.index("--chdir") + 1] == str(tmp_path / "bundle")


def test_read_paths_are_read_only_write_paths_read_write_and_write_wins(tmp_path):
    only_read, both, only_write = tmp_path / "r", tmp_path / "b", tmp_path / "w"
    argv = _command(_plan(tmp_path, read=[only_read, both], write=[both, only_write]))
    assert str(only_read) in _mounts(argv, "--ro-bind-try")
    assert str(only_write) in _mounts(argv, "--bind-try")
    assert str(both) in _mounts(argv, "--bind-try")
    assert str(both) not in _mounts(argv, "--ro-bind-try")
    assert _mounts(argv, "--bind") == [str(tmp_path / "run" / "net")]


def test_a_parent_is_mounted_before_the_child_inside_it(tmp_path):
    parent, child = tmp_path / "a", tmp_path / "a" / "b" / "c"
    # The child is listed first, and read-only under a writable parent.
    argv = _command(_plan(tmp_path, read=[child], write=[parent]))
    flags = {argv[i + 1]: a for i, a in enumerate(argv) if a in ("--ro-bind-try", "--bind-try")}
    order = [argv[i + 1] for i, a in enumerate(argv) if a in ("--ro-bind-try", "--bind-try")]
    assert order.index(str(parent)) < order.index(str(child))
    assert flags[str(parent)] == "--bind-try" and flags[str(child)] == "--ro-bind-try"


def test_every_mount_comes_after_the_tmpfs_that_would_hide_it(tmp_path):
    argv = _command(_plan(tmp_path, read=[tmp_path / "r"], write=[tmp_path / "w"]))
    last_tmpfs = max(i for i, a in enumerate(argv) if a == "--tmpfs")
    first_bind = min(
        i for i, a in enumerate(argv) if a in ("--ro-bind-try", "--bind-try", "--bind")
    )
    assert last_tmpfs < first_bind


def test_what_the_person_keeps_in_home_run_and_tmp_is_replaced_by_an_empty_tmpfs(tmp_path):
    elsewhere = tmp_path / "elsewhere-home"
    elsewhere.mkdir()
    argv = _command(_plan(tmp_path), home=elsewhere)
    hidden = _mounts(argv, "--tmpfs")
    # Home is hidden wherever it lives; the fixed directories only if they exist.
    assert str(elsewhere) in hidden
    assert hidden.count(str(elsewhere)) == 1
    for fixed in bubblewrap.HIDDEN:
        assert (fixed in hidden) == Path(fixed).exists()
    # And each is read-only once the grants are mounted inside it.
    assert _mounts(argv, "--remount-ro") == hidden
    mounted = [i for i, a in enumerate(argv) if a in ("--bind-try", "--ro-bind-try", "--bind")]
    assert max(mounted) < min(i for i, a in enumerate(argv) if a == "--remount-ro")


def test_a_home_under_a_hidden_directory_is_not_hidden_twice(tmp_path):
    hidden_root = next((p for p in bubblewrap.HIDDEN if Path(p).exists()), None)
    if hidden_root is None:
        pytest.skip("none of the hidden directories exist here")
    argv = _command(_plan(tmp_path), home=Path(hidden_root) / "someone")
    hidden = _mounts(argv, "--tmpfs")
    assert hidden.count(hidden_root) == 1
    assert str(Path(hidden_root) / "someone") not in hidden


def test_a_hidden_directory_that_does_not_exist_is_skipped(tmp_path):
    ghost = tmp_path / "no-such-home"
    argv = _command(_plan(tmp_path), home=ghost)
    assert str(ghost) not in _mounts(argv, "--tmpfs")
    assert str(ghost) not in _mounts(argv, "--remount-ro")


def test_the_bridge_interpreter_is_readable_wherever_it_lives(tmp_path):
    python_root = tmp_path / "py"
    (python_root / "bin").mkdir(parents=True)
    exe = python_root / "bin" / "python3"
    exe.write_text("")
    # An interpreter under the home directory is not reachable by default.
    argv = _command(_plan(tmp_path), bridge_python=str(exe), home=tmp_path)
    assert str(python_root) in _mounts(argv, "--ro-bind-try")


def test_paths_with_spaces_and_unicode_are_single_arguments(tmp_path):
    odd = tmp_path / "my docs é"
    argv = _command(_plan(tmp_path, read=[odd]))
    assert [str(odd), str(odd)] == argv[argv.index(str(odd)) : argv.index(str(odd)) + 2]
    assert argv[argv.index(str(odd)) - 1] == "--ro-bind-try"

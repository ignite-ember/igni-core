"""One interface for the sandboxes, and what each prepares and undoes.

The platform probes and the programs they would run are replaced, so
every mechanism's own preparation is pinned on every host: which
mechanism a platform name gets, why there is none, what the proxy and
the loopback relays are told, and what is left running when a
preparation fails half way. The real sandboxes are pinned by the tests
that run on their own operating system.
"""

from __future__ import annotations

import asyncio
import shutil
import stat
import tempfile
import threading
from pathlib import Path

import pytest

from igni_core.extensions.sandbox import (
    EgressProxy,
    HostAllowList,
    MechanismDeps,
    MechanismName,
    MechanismRun,
    SandboxAvailability,
    SandboxMechanism,
    SandboxPlan,
)
from igni_core.extensions.sandbox.appcontainer import AppContainer, ContainerRun
from igni_core.extensions.sandbox.bubblewrap import PROXY_PORT, Bubblewrap
from igni_core.extensions.sandbox.mechanism import (
    MECHANISMS,
    AppContainerMechanism,
    BubblewrapMechanism,
    SeatbeltMechanism,
    probe_platform,
)
from igni_core.extensions.sandbox.seatbelt import Seatbelt

pytestmark = pytest.mark.extension_sandbox


@pytest.fixture
def probes(monkeypatch):
    """Every platform's probe answers what the test says; ``None`` is a working sandbox."""
    answers: dict[str, str | None] = {"seatbelt": None, "bubblewrap": None, "appcontainer": None}

    async def seatbelt():
        return answers["seatbelt"]

    async def bubblewrap():
        return answers["bubblewrap"]

    async def appcontainer():
        return answers["appcontainer"]

    monkeypatch.setattr(Seatbelt, "probe", staticmethod(seatbelt))
    monkeypatch.setattr(Bubblewrap, "probe", classmethod(lambda cls: bubblewrap()))
    monkeypatch.setattr(AppContainer, "probe", staticmethod(appcontainer))
    return answers


@pytest.mark.parametrize(
    ("platform", "mechanism", "name"),
    [
        ("darwin", SeatbeltMechanism, MechanismName.SEATBELT),
        ("linux", BubblewrapMechanism, MechanismName.BUBBLEWRAP),
        ("linux2", BubblewrapMechanism, MechanismName.BUBBLEWRAP),
        ("linux-gnu", BubblewrapMechanism, MechanismName.BUBBLEWRAP),
        ("win32", AppContainerMechanism, MechanismName.APPCONTAINER),
    ],
)
async def test_a_platform_gets_the_mechanism_that_serves_it(probes, platform, mechanism, name):
    found = await probe_platform(platform)
    assert type(found.mechanism) is mechanism
    assert found.name is name and name.value in ("seatbelt", "bubblewrap", "appcontainer")
    assert found.reason == ""


@pytest.mark.parametrize("platform", ["sunos5", "freebsd14", "", "cygwin", "Linux", "windows"])
async def test_a_platform_with_no_mechanism_says_so(probes, platform):
    found = await probe_platform(platform)
    assert found.mechanism is None and found.name is None
    assert found.reason == f"no sandbox for {platform}"


@pytest.mark.parametrize(
    ("platform", "which", "why"),
    [
        ("darwin", "seatbelt", "sandbox-exec is not here"),
        ("linux", "bubblewrap", "bwrap is not installed"),
        ("win32", "appcontainer", "Windows build 9600 is too old"),
    ],
)
async def test_a_probe_that_fails_gives_its_reason_and_no_mechanism(probes, platform, which, why):
    probes[which] = why
    found = await probe_platform(platform)
    assert found.mechanism is None and found.name is None and found.reason == why


def test_every_platform_is_served_by_exactly_one_mechanism():
    assert [m.name for m in MECHANISMS] == list(MechanismName)
    for platform in ("darwin", "linux", "win32"):
        assert sum(m.serves(platform) for m in MECHANISMS) == 1
    assert not any(m.serves("sunos5") for m in MECHANISMS)


async def test_the_availability_name_is_the_mechanism_s_or_none(probes):
    assert SandboxAvailability().name is None
    assert (await probe_platform("linux")).name is MechanismName.BUBBLEWRAP


async def test_windows_makes_its_mechanism_with_the_container_igni_gives_it(probes):
    given = AppContainer()
    found = await probe_platform("win32", MechanismDeps(appcontainer=given))
    assert found.mechanism._appcontainer is given
    # Without any, it makes its own; the other platforms take nothing.
    alone = await probe_platform("win32")
    assert isinstance(alone.mechanism._appcontainer, AppContainer)
    assert alone.mechanism._appcontainer is not given
    assert isinstance(
        (await probe_platform("linux", MechanismDeps(appcontainer=given))).mechanism,
        BubblewrapMechanism,
    )


async def test_a_mechanism_has_nothing_to_undo_unless_it_says_otherwise():
    class Plain(SandboxMechanism):
        name = MechanismName.SEATBELT

        @classmethod
        def serves(cls, platform):
            return True

        async def probe(self):
            return None

        async def prepare(self, plan, proxy, *, command, args, cwd):
            raise AssertionError("not asked")

    assert await Plain().release(MechanismRun(command="x", proxy_port=1)) is None


# ── what each one prepares ────────────────────────────────────────────


def a_plan(run_dir: Path, *, ports: list[int] | None = None) -> SandboxPlan:
    run_dir.mkdir(parents=True, exist_ok=True)
    plan = SandboxPlan(extension_id="acme.probe", run_dir=run_dir)
    plan.loopback_ports = list(ports or [])
    return plan


@pytest.fixture
def short_dir():
    """A directory whose path is short enough for a unix socket on every host."""
    path = Path(tempfile.mkdtemp(prefix="ig", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def a_proxy() -> EgressProxy:
    return EgressProxy("acme.probe", HostAllowList(), token="t")


async def refuses(port: int) -> bool:
    try:
        _, writer = await asyncio.open_connection("127.0.0.1", port)
    except OSError:
        return True
    writer.close()
    return False


async def test_seatbelt_denies_every_igni_listener_it_was_not_granted(monkeypatch, tmp_path):
    seen: dict = {}

    async def listening(pid=None):
        return {1111, 2222, 3333}

    def command(plan, cmd, args):
        seen["denied"] = list(plan.denied_ports)
        return "sandbox-exec", ["-p", "profile", cmd, *args]

    monkeypatch.setattr(Seatbelt, "listening_ports", staticmethod(listening))
    monkeypatch.setattr(Seatbelt, "command", classmethod(lambda cls, p, c, a: command(p, c, a)))
    plan = a_plan(tmp_path / "run", ports=[2222])
    proxy = a_proxy()
    try:
        run = await SeatbeltMechanism().prepare(plan, proxy, command="tool", args=["a"], cwd=None)
        # The proxy's port is told, a granted port is not denied, the others are, in order.
        assert run.proxy_port == proxy.port and run.proxy_port > 0
        assert seen["denied"] == [1111, 3333] and plan.denied_ports == [1111, 3333]
        assert run.command == "sandbox-exec" and run.args == ["-p", "profile", "tool", "a"]
        assert run.relays == [] and run.container is None
    finally:
        await proxy.stop()


async def test_seatbelt_does_not_deny_its_own_proxy_port(monkeypatch, tmp_path):
    proxy = a_proxy()
    original = proxy.start_tcp
    listening: set[int] = {4444}

    async def start():
        port = await original()
        listening.add(port)  # the proxy's own port is listening by the time igni lists them
        return port

    async def listing(pid=None):
        return set(listening)

    proxy.start_tcp = start
    monkeypatch.setattr(Seatbelt, "listening_ports", staticmethod(listing))
    monkeypatch.setattr(Seatbelt, "command", classmethod(lambda cls, p, c, a: (c, a)))
    plan = a_plan(tmp_path / "run")
    try:
        await SeatbeltMechanism().prepare(plan, proxy, command="tool", args=[], cwd=None)
        assert plan.denied_ports == [4444]
    finally:
        await proxy.stop()


async def test_bubblewrap_bridges_the_proxy_and_each_loopback_port_through_a_private_directory(
    monkeypatch, short_dir
):
    monkeypatch.setattr(
        Bubblewrap,
        "command",
        classmethod(lambda cls, plan, c, a, cwd=None, **kw: ("bwrap", ["--", c, *a])),
    )
    plan = a_plan(short_dir / "run", ports=[41001, 41002])
    proxy = a_proxy()
    run = None
    try:
        run = await BubblewrapMechanism().prepare(
            plan, proxy, command="tool", args=["x"], cwd=short_dir
        )
        net = Bubblewrap.socket_dir(plan)
        assert stat.S_IMODE(net.stat().st_mode) == 0o700
        assert Bubblewrap.proxy_socket(plan).exists()
        assert [r.port for r in run.relays] == [41001, 41002]
        assert [r.path for r in run.relays] == [
            Bubblewrap.relay_socket(plan, 41001),
            Bubblewrap.relay_socket(plan, 41002),
        ]
        assert all(r.path.exists() for r in run.relays)
        assert run.proxy_port == PROXY_PORT
        assert run.command == "bwrap" and run.args == ["--", "tool", "x"]
    finally:
        await proxy.stop()
        if run is not None:
            for relay in run.relays:
                await relay.stop()


async def test_bubblewrap_with_no_loopback_port_relays_nothing(monkeypatch, short_dir):
    monkeypatch.setattr(
        Bubblewrap, "command", classmethod(lambda cls, plan, c, a, cwd=None, **kw: (c, a))
    )
    plan = a_plan(short_dir / "run")
    Bubblewrap.socket_dir(plan).mkdir()  # already there, as after an earlier run
    proxy = a_proxy()
    try:
        run = await BubblewrapMechanism().prepare(plan, proxy, command="t", args=[], cwd=None)
        assert run.relays == []
    finally:
        await proxy.stop()


async def test_a_bubblewrap_command_that_cannot_be_written_leaves_no_relay_up(
    monkeypatch, short_dir
):
    def broken(cls, *a, **kw):
        raise RuntimeError("no command line")

    monkeypatch.setattr(Bubblewrap, "command", classmethod(broken))
    plan = a_plan(short_dir / "run", ports=[41001, 41002])
    proxy = a_proxy()
    started: list = []
    from igni_core.extensions.sandbox import proxy as proxy_module

    original = proxy_module.LoopbackRelay.start

    async def start(self):
        await original(self)
        started.append(self)

    monkeypatch.setattr(proxy_module.LoopbackRelay, "start", start)
    try:
        with pytest.raises(RuntimeError, match="no command line"):
            await BubblewrapMechanism().prepare(plan, proxy, command="t", args=[], cwd=None)
        assert len(started) == 2
        for relay in started:
            assert relay._server is None
            with pytest.raises(OSError):
                _, writer = await asyncio.open_unix_connection(str(relay.path))
                writer.close()
    finally:
        await proxy.stop()


async def test_a_relay_that_cannot_start_stops_the_ones_before_it(monkeypatch, short_dir):
    from igni_core.extensions.sandbox import proxy as proxy_module

    original = proxy_module.LoopbackRelay.start
    started: list = []

    async def start(self):
        if self.port == 41002:
            raise OSError("address in use")
        await original(self)
        started.append(self)

    monkeypatch.setattr(proxy_module.LoopbackRelay, "start", start)
    plan = a_plan(short_dir / "run", ports=[41001, 41002])
    proxy = a_proxy()
    try:
        with pytest.raises(OSError, match="address in use"):
            await BubblewrapMechanism().prepare(plan, proxy, command="t", args=[], cwd=None)
        assert [r.port for r in started] == [41001] and started[0]._server is None
    finally:
        await proxy.stop()


class FakeContainer:
    """An AppContainer whose Win32 work is a record."""

    def __init__(self) -> None:
        self.started: list[dict] = []
        self.stopped: list[ContainerRun] = []
        self.threads: list[int] = []
        self.run = ContainerRun(profile="p", sid="S-1", owner=1)

    def start(self, plan, command, args, *, cwd, forwards):
        self.threads.append(threading.get_ident())
        self.started.append({"command": command, "args": args, "cwd": cwd, "forwards": forwards})
        return self.run, "runner.exe", ["run", command, *args]

    def stop(self, container):
        self.threads.append(threading.get_ident())
        self.stopped.append(container)


async def test_appcontainer_starts_the_container_off_the_loop_with_the_proxy_and_ports_forwarded(
    tmp_path,
):
    fake = FakeContainer()
    plan = a_plan(tmp_path / "run", ports=[41001, 41002])
    proxy = a_proxy()
    mechanism = AppContainerMechanism(fake)
    try:
        run = await mechanism.prepare(plan, proxy, command="tool", args=["a"], cwd=tmp_path)
        assert fake.started == [
            {
                "command": "tool",
                "args": ["a"],
                "cwd": tmp_path,
                "forwards": [proxy.port, 41001, 41002],
            }
        ]
        assert threading.get_ident() not in fake.threads
        assert run.command == "runner.exe" and run.args == ["run", "tool", "a"]
        assert run.proxy_port == proxy.port and run.container is fake.run
        assert run.env == AppContainer.environment(plan)
        assert run.relays == []
        await mechanism.release(run)
        assert fake.stopped == [fake.run]
        assert threading.get_ident() not in fake.threads
    finally:
        await proxy.stop()


async def test_appcontainer_has_nothing_to_undo_for_a_run_without_a_container():
    fake = FakeContainer()
    await AppContainerMechanism(fake).release(MechanismRun(command="x", proxy_port=1))
    assert fake.stopped == []


async def test_a_failing_appcontainer_start_is_raised_as_it_is(tmp_path):
    class Failing(FakeContainer):
        def start(self, *a, **kw):
            raise PermissionError(5, "Access is denied")

    proxy = a_proxy()
    try:
        with pytest.raises(PermissionError):
            await AppContainerMechanism(Failing()).prepare(
                a_plan(tmp_path / "run"), proxy, command="t", args=[], cwd=None
            )
    finally:
        await proxy.stop()

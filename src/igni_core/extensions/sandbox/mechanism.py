"""The sandboxes igni can confine an extension's process with, and the one interface they share.

macOS confines with Seatbelt (:mod:`.seatbelt`), Linux with bubblewrap
(:mod:`.bubblewrap`), Windows with an AppContainer of the run's own
(:mod:`.appcontainer`). All three answer the same two questions — can
this machine confine a process at all, and what has to be ready before
the process starts — so they answer through one interface
(:class:`SandboxMechanism`), and the launcher (:mod:`.launcher`) asks
for a sandbox without naming any of them.

A mechanism is asked three times: once, when igni starts, whether this
machine has one at all (:meth:`SandboxMechanism.probe`), once per
extension, to make the sandbox ready for that process
(:meth:`SandboxMechanism.prepare`), and once when the run ends, to undo
what it made ready (:meth:`SandboxMechanism.release`). What it made
ready is one value (:class:`MechanismRun`): the command to run, the port
the egress proxy answers on inside the sandbox, the loopback relays
bridged in, and whatever else the platform needs.
"""

from __future__ import annotations

import abc
import asyncio
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from igni_core.extensions.sandbox.appcontainer import AppContainer, ContainerRun
from igni_core.extensions.sandbox.bubblewrap import PROXY_PORT, Bubblewrap
from igni_core.extensions.sandbox.plan import SandboxPlan
from igni_core.extensions.sandbox.proxy import EgressProxy, LoopbackRelay, stop_relays
from igni_core.extensions.sandbox.seatbelt import Seatbelt


class MechanismName(StrEnum):
    """Which sandbox confines a process."""

    SEATBELT = "seatbelt"
    BUBBLEWRAP = "bubblewrap"
    APPCONTAINER = "appcontainer"


@dataclass(frozen=True)
class MechanismDeps:
    """What a mechanism may need from igni when igni makes one.

    A mechanism that works with something of igni's — Windows' mechanism
    grants its paths with an AppContainer — is given it here, so that
    the launcher need not name a platform to say so.
    """

    appcontainer: AppContainer | None = None


class MechanismRun(BaseModel):
    """What a mechanism made ready for one process."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    #: The command to run, sandboxed.
    command: str
    #: The arguments it runs with.
    args: list[str] = Field(default_factory=list)
    #: The port the egress proxy answers on inside the sandbox.
    proxy_port: int
    #: The loopback ports bridged into the sandbox, and the relays for them.
    relays: list[LoopbackRelay] = Field(default_factory=list)
    #: The environment the platform sets for the process itself: Windows'
    #: own names for home and app data.
    env: dict[str, str] = Field(default_factory=dict)
    #: Windows: the run's AppContainer and what it was granted, to undo.
    container: ContainerRun | None = None


class SandboxMechanism(abc.ABC):
    """One platform's way of confining a process."""

    #: The name told to the process (``IGNI_SANDBOX``).
    name: ClassVar[MechanismName]

    @classmethod
    def create(cls, deps: MechanismDeps) -> SandboxMechanism:
        """The mechanism, with what it needs from igni."""
        return cls()

    @classmethod
    @abc.abstractmethod
    def serves(cls, platform: str) -> bool:
        """Whether this platform is the one this mechanism confines for."""

    @abc.abstractmethod
    async def probe(self) -> str | None:
        """``None`` when this machine can confine a process with it, else why not."""

    @abc.abstractmethod
    async def prepare(
        self,
        plan: SandboxPlan,
        proxy: EgressProxy,
        *,
        command: str,
        args: list[str],
        cwd: Path | None,
    ) -> MechanismRun:
        """Make the sandbox ready, and say how the process runs inside it."""

    async def release(self, prepared: MechanismRun) -> None:
        """Undo what :meth:`prepare` made ready. Nothing to undo by default."""
        return None


class SeatbeltMechanism(SandboxMechanism):
    """macOS: Seatbelt, through ``sandbox-exec`` and a generated profile."""

    name = MechanismName.SEATBELT

    @classmethod
    def serves(cls, platform: str) -> bool:
        return platform == "darwin"

    async def probe(self) -> str | None:
        return await Seatbelt.probe()

    async def prepare(
        self,
        plan: SandboxPlan,
        proxy: EgressProxy,
        *,
        command: str,
        args: list[str],
        cwd: Path | None,
    ) -> MechanismRun:
        """The proxy on a loopback port of its own, and igni's other listeners denied.

        Seatbelt cannot filter loopback by port for a server the process
        starts on a port it picks, so igni's own listeners are denied by
        name: the ones the plan grants, and the rest.
        """
        port = await proxy.start_tcp()
        granted = {port, *plan.loopback_ports}
        plan.denied_ports = sorted((await Seatbelt.listening_ports()) - granted)
        wrapped, argv = Seatbelt.command(plan, command, args)
        return MechanismRun(command=wrapped, args=argv, proxy_port=port)


class BubblewrapMechanism(SandboxMechanism):
    """Linux: bubblewrap, with a network namespace of its own."""

    name = MechanismName.BUBBLEWRAP

    @classmethod
    def serves(cls, platform: str) -> bool:
        return platform.startswith("linux")

    async def probe(self) -> str | None:
        return await Bubblewrap.probe()

    async def prepare(
        self,
        plan: SandboxPlan,
        proxy: EgressProxy,
        *,
        command: str,
        args: list[str],
        cwd: Path | None,
    ) -> MechanismRun:
        """The proxy on a unix socket bridged in, and igni's loopback ports relayed.

        The process's loopback is its own, so igni's host channel and
        token endpoint are relayed in through unix sockets.
        """
        Bubblewrap.socket_dir(plan).mkdir(mode=0o700, exist_ok=True)
        await proxy.start_unix(Bubblewrap.proxy_socket(plan))
        relays: list[LoopbackRelay] = []
        try:
            for port in plan.loopback_ports:
                relay = LoopbackRelay(port, Bubblewrap.relay_socket(plan, port))
                await relay.start()
                relays.append(relay)
            wrapped, argv = Bubblewrap.command(plan, command, args, cwd=cwd)
        except Exception:
            # A relay that is up must not be left behind by a command
            # line that could not be written.
            await stop_relays(relays)
            raise
        return MechanismRun(command=wrapped, args=argv, proxy_port=PROXY_PORT, relays=relays)


class AppContainerMechanism(SandboxMechanism):
    """Windows: an AppContainer of the run's own, in a job object."""

    name = MechanismName.APPCONTAINER

    def __init__(self, appcontainer: AppContainer | None = None) -> None:
        self._appcontainer = appcontainer if appcontainer is not None else AppContainer()

    @classmethod
    def create(cls, deps: MechanismDeps) -> SandboxMechanism:
        return cls(deps.appcontainer)

    @classmethod
    def serves(cls, platform: str) -> bool:
        return platform == "win32"

    async def probe(self) -> str | None:
        return await self._appcontainer.probe()

    async def prepare(
        self,
        plan: SandboxPlan,
        proxy: EgressProxy,
        *,
        command: str,
        args: list[str],
        cwd: Path | None,
    ) -> MechanismRun:
        """A container of the run's own, with igni's ports and the proxy in it.

        The container has no network capability at all, so what it may
        reach it reaches through named pipes: the proxy's port and
        igni's own loopback ports are forwarded to it, and the shim
        (:mod:`.winbridge`) inside it listens on ports of its own.
        """
        port = await proxy.start_tcp()
        container, runner, argv = await asyncio.to_thread(
            self._appcontainer.start,
            plan,
            command,
            args,
            cwd=cwd,
            forwards=[port, *plan.loopback_ports],
        )
        return MechanismRun(
            command=runner,
            args=argv,
            proxy_port=port,
            env=AppContainer.environment(plan),
            container=container,
        )

    async def release(self, prepared: MechanismRun) -> None:
        """Its entries come off the person's files; a failure stays in the ledger."""
        if prepared.container is None:
            return
        await asyncio.to_thread(self._appcontainer.stop, prepared.container)


class SandboxAvailability(BaseModel):
    """Whether this machine has a sandbox, and which one."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    #: The mechanism that will confine a process; ``None`` where there is none.
    mechanism: SandboxMechanism | None = None
    #: Why there is none; empty when there is one.
    reason: str = ""

    @property
    def name(self) -> MechanismName | None:
        """The name told to the process; ``None`` where there is no sandbox."""
        return self.mechanism.name if self.mechanism is not None else None


#: The mechanisms igni has, each saying which platforms it serves.
MECHANISMS: tuple[type[SandboxMechanism], ...] = (
    SeatbeltMechanism,
    BubblewrapMechanism,
    AppContainerMechanism,
)


async def probe_platform(platform: str, deps: MechanismDeps | None = None) -> SandboxAvailability:
    """The sandbox this platform has here, and whether it works."""
    for candidate in MECHANISMS:
        if candidate.serves(platform):
            mechanism = candidate.create(deps or MechanismDeps())
            reason = await mechanism.probe()
            if reason is None:
                return SandboxAvailability(mechanism=mechanism)
            return SandboxAvailability(reason=reason)
    return SandboxAvailability(reason=_no_mechanism(platform))


def _no_mechanism(platform: str) -> str:
    """Why this platform has no sandbox to confine a process with."""
    return f"no sandbox for {platform}"


__all__ = [
    "MECHANISMS",
    "AppContainerMechanism",
    "BubblewrapMechanism",
    "MechanismDeps",
    "MechanismName",
    "MechanismRun",
    "SandboxAvailability",
    "SandboxMechanism",
    "SeatbeltMechanism",
    "probe_platform",
]

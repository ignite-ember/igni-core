"""The pool resumes each session onto the server class it was given.

The seam used to be a class attribute — ``SessionRuntimeFactory.
server_cls`` — which every instance of a factory, and every subclass,
shared. A test that wanted one factory on a stand-in server and
another on a different one could not say so: the last assignment won
for both, and a factory built in another test could find the class
attribute changed under it. The seam is a constructor argument now,
so two factories run side by side on the servers they were each
given, and nothing a test does to one of them reaches the other.

What we pin:

* the factory carries no class-level server seam;
* the default is the real :class:`BackendServer`, which is what
  production resumes onto;
* two factories built on different server classes each resume onto
  their own.
"""

from __future__ import annotations

import inspect
from dataclasses import MISSING
from typing import Any

import pytest

from igni_core.backend.login_coordinator import LoginCoordinator
from igni_core.backend.pool_directories import PoolDirectories
from igni_core.backend.pool_wiring import PoolWiring
from igni_core.backend.push_bridge import PushNotificationBridge
from igni_core.backend.schemas_pool import ProjectDirs
from igni_core.backend.server import BackendServer
from igni_core.backend.session_runtime_factory import SessionRuntimeFactory
from igni_core.core.config.group_policy.gate import RunGate
from igni_core.core.config.settings import Settings
from igni_core.core.db.locator import StorageLocator
from igni_core.core.session.broadcast import BroadcastBus
from igni_core.core.session.session_directories import SessionDirectoryStore
from igni_core.core.tools.process_supervisor import ProcessSupervisor
from igni_core.core.tools.process_supervisor_locator import SupervisorLocator
from tests.shared_tables import SharedTables


class _StandIn:
    """A backend that is not a :class:`BackendServer`, for the pool to resume onto."""

    def __init__(
        self,
        settings: Any,
        *,
        project_dir: Any,
        resume_session_id: str,
        additional_dirs: Any = None,
        contributions: Any = None,
        prompt_loader: Any = None,
        tool_hints: Any = None,
        supervisors: SupervisorLocator | None = None,
        gate: RunGate | None = None,
    ) -> None:
        self.session_id = resume_session_id
        self.project_dir = project_dir
        self.message_queue: list[str] = []
        self.broadcast_bus = BroadcastBus()
        self._supervisors = supervisors if supervisors is not None else SupervisorLocator()
        self.gate = gate if gate is not None else RunGate()
        # The process's tables, as this server was handed them.
        self.contributions = contributions
        self.prompt_loader = prompt_loader
        self.tool_hints = tool_hints

    @property
    def supervisor(self) -> ProcessSupervisor:
        return self._supervisors.supervisor

    async def startup(self) -> None:
        pass

    def wire_queue_hook(self, queue: Any) -> None:
        pass

    def register_broadcast(self, callback: Any) -> None:
        self.broadcast_bus.register(callback)

    def attach_runtime(self, runtime: Any) -> None:
        self.runtime = runtime


class _OtherStandIn(_StandIn):
    """A second stand-in, so the two factories cannot be the same object."""


class _Transport:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, message: Any) -> None:
        self.sent.append(message)

    async def receive(self):
        if False:  # pragma: no cover — the pool never reads from this
            yield None


def _factory(tmp_path, server_cls) -> SessionRuntimeFactory:
    settings = Settings(storage={"data_dir": str(tmp_path / "data")})
    transport = _Transport()
    push = PushNotificationBridge(transport=transport, loop=None, queue=[])
    login = LoginCoordinator(backend=None, push_bridge=push)
    tables = SharedTables()
    directories = PoolDirectories(
        SessionDirectoryStore.from_locator(StorageLocator.from_settings(settings)),
        ProjectDirs(project_dir=tmp_path),
    )
    return SessionRuntimeFactory(
        wiring=PoolWiring(
            transport=transport,
            push_bridge=push,
            login=login,
            supervisors=SupervisorLocator(),
            gate=RunGate(),
            contributions=tables.contributions,
            prompt_loader=tables.prompt_loader,
            tool_hints=tables.tool_hints,
            extensions=tables.extensions,
        ),
        settings=settings,
        dirs=directories,
        server_cls=server_cls,
    )


class TestTheServerSeam:
    def test_the_factory_carries_no_class_level_server_seam(self) -> None:
        """No ``server_cls`` on the class: the seam is per instance.

        A class attribute is a global wearing a class's clothes — one
        test rebinding it changes every factory, including the ones
        another test is holding.
        """
        assert not hasattr(SessionRuntimeFactory, "server_cls")

    def test_the_default_is_the_real_backend_server(self) -> None:
        """Production resumes onto the real :class:`BackendServer`."""
        default = inspect.signature(SessionRuntimeFactory.__init__).parameters["server_cls"].default
        assert default is BackendServer


class TestEachFactoryResumesOntoItsOwn:
    @pytest.mark.asyncio
    async def test_two_factories_resume_onto_two_servers(self, tmp_path) -> None:
        """Two factories, two server classes, each onto its own.

        Under the old class-attribute seam this could not be
        expressed: the second factory's assignment would have changed
        the first one's server too.
        """
        first = _factory(tmp_path / "a", _StandIn)
        second = _factory(tmp_path / "b", _OtherStandIn)

        first_runtime = await first.create("sess-a")
        second_runtime = await second.create("sess-b")

        assert type(first_runtime.backend) is _StandIn
        assert type(second_runtime.backend) is _OtherStandIn

    @pytest.mark.asyncio
    async def test_one_factory_does_not_reach_the_other(self, tmp_path) -> None:
        """The two factories' servers are separate objects."""
        first = _factory(tmp_path / "a", _StandIn)
        second = _factory(tmp_path / "b", _OtherStandIn)

        first_runtime = await first.create("sess-a")
        second_runtime = await second.create("sess-b")

        assert first_runtime.backend is not second_runtime.backend


class TestTheSharedTables:
    """One table per process, not one per session.

    The tables the extensions write into — what they did to the
    prompts, the anchors they fill, what their tools say they are —
    are the process's, not the session's. A wiring that carried a
    default would carry a *private* table, and the extension system
    would write into one while the session read another.
    """

    def test_the_shared_tables_have_no_defaults(self) -> None:
        """Required, not defaulted."""
        for name in ("contributions", "prompt_loader", "tool_hints", "extensions"):
            field = PoolWiring.__dataclass_fields__[name]
            assert field.default is MISSING, f"{name} has a default of its own"
            assert field.default_factory is MISSING, f"{name} builds a default of its own"

    @pytest.mark.asyncio
    async def test_every_session_of_a_pool_gets_the_process_tables(self, tmp_path) -> None:
        """Two sessions of one pool, one set of tables."""
        factory = _factory(tmp_path / "a", _StandIn)

        first = await factory.create("sess-a")
        second = await factory.create("sess-b")

        assert first.backend.contributions is second.backend.contributions
        assert first.backend.prompt_loader is second.backend.prompt_loader
        assert first.backend.tool_hints is second.backend.tool_hints

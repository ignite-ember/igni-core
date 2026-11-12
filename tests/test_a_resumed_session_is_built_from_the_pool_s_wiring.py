"""What the session runtime factory hands each session it builds.

A resumed session gets its own deep copy of the settings (resuming
applies the session's model preference, which must not leak into the
process's), its own directory, queue, router and rpc table, and the
process-wide supervisors and gate. Its directory is recorded once the
server has started, not before; the boot session is wrapped on a
transport that stamps its id.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from igni_core.backend.login_coordinator import LoginCoordinator
from igni_core.backend.pool_directories import PoolDirectories
from igni_core.backend.pool_wiring import PoolWiring
from igni_core.backend.push_bridge import PushNotificationBridge
from igni_core.backend.schemas_pool import ProjectDirs
from igni_core.backend.session_runtime_factory import BootRuntime, SessionRuntimeFactory
from igni_core.backend.session_stamping_transport import SessionStampingTransport
from igni_core.core.config.group_policy.gate import RunGate
from igni_core.core.config.settings import Settings
from igni_core.core.db.locator import StorageLocator
from igni_core.core.session.broadcast import BroadcastBus
from igni_core.core.session.session_directories import SessionDirectoryStore
from igni_core.core.tools.process_supervisor_locator import SupervisorLocator
from tests.shared_tables import SharedTables


class _Server:
    """Records what it was built with; ``fail_startup`` makes it refuse to start."""

    fail_startup = False
    built: list[_Server] = []

    def __init__(self, settings: Settings, **kwargs: Any) -> None:
        self.settings = settings
        self.kwargs = kwargs
        self.session_id = kwargs["resume_session_id"]
        self.project_dir = kwargs["project_dir"]
        self.message_queue: list[str] = []
        self.broadcast_bus = BroadcastBus()
        self.queue_hook: Any = None
        self.runtime: Any = None
        self.supervisor = kwargs["supervisors"].supervisor if "supervisors" in kwargs else None
        self.gate = kwargs.get("gate")
        _Server.built.append(self)

    async def startup(self) -> None:
        if _Server.fail_startup:
            raise RuntimeError("cannot start")
        # Resuming applies the session's own model preference.
        self.settings.models.default = "resumed-model"

    def wire_queue_hook(self, queue: Any) -> None:
        self.queue_hook = queue

    def register_broadcast(self, callback: Any) -> None:
        self.broadcast_bus.register(callback)

    def attach_runtime(self, runtime: Any) -> None:
        self.runtime = runtime


class _Transport:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, message: Any) -> None:
        self.sent.append(message)


@pytest.fixture(autouse=True)
def _reset():
    _Server.built = []
    _Server.fail_startup = False


def _rig(tmp_path: Path):
    settings = Settings(
        storage={"data_dir": str(tmp_path / "data")}, models={"default": "boot-model"}
    )
    store = SessionDirectoryStore.from_locator(StorageLocator.from_settings(settings))
    project = tmp_path / "project"
    project.mkdir()
    extra = tmp_path / "extra"
    extra.mkdir()
    dirs = PoolDirectories(store, ProjectDirs(project_dir=project, additional_dirs=[extra]))
    transport = _Transport()
    push = PushNotificationBridge(transport=transport, loop=None, queue=[])
    tables = SharedTables()
    wiring = PoolWiring(
        transport=transport,
        push_bridge=push,
        login=LoginCoordinator(backend=None, push_bridge=push),
        supervisors=SupervisorLocator(),
        gate=RunGate(),
        contributions=tables.contributions,
        prompt_loader=tables.prompt_loader,
        tool_hints=tables.tool_hints,
        extensions=tables.extensions,
    )
    factory = SessionRuntimeFactory(wiring=wiring, settings=settings, dirs=dirs, server_cls=_Server)  # type: ignore[arg-type]
    return SimpleNamespace(
        factory=factory,
        settings=settings,
        store=store,
        dirs=dirs,
        wiring=wiring,
        transport=transport,
        project=project,
        extra=extra,
    )


async def test_a_resumed_session_gets_its_own_copy_of_the_settings(tmp_path) -> None:
    rig = _rig(tmp_path)

    await rig.factory.create("sess-a")
    await rig.factory.create("sess-b")

    first, second = _Server.built
    assert first.settings is not rig.settings
    assert first.settings is not second.settings
    assert first.settings.models.default == "resumed-model"
    assert rig.settings.models.default == "boot-model"


async def test_a_resumed_session_is_handed_the_process_wiring(tmp_path) -> None:
    rig = _rig(tmp_path)

    await rig.factory.create("sess-a")

    kwargs = _Server.built[0].kwargs
    assert kwargs["resume_session_id"] == "sess-a"
    assert kwargs["project_dir"] == rig.project
    assert kwargs["additional_dirs"] == [rig.extra]
    assert kwargs["contributions"] is rig.wiring.contributions
    assert kwargs["prompt_loader"] is rig.wiring.prompt_loader
    assert kwargs["tool_hints"] is rig.wiring.tool_hints
    assert kwargs["supervisors"] is rig.wiring.supervisors
    assert kwargs["gate"] is rig.wiring.gate


async def test_a_session_resumes_in_the_directory_it_was_recorded_in(tmp_path) -> None:
    rig = _rig(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    rig.dirs.record("sess-a", elsewhere)

    runtime = await rig.factory.create("sess-a")

    assert _Server.built[0].kwargs["project_dir"] == elsewhere
    assert runtime.backend.project_dir == elsewhere
    assert Path(rig.store.get_dir("sess-a")) == elsewhere


async def test_a_session_whose_directory_is_gone_resumes_in_the_backends_own(tmp_path) -> None:
    rig = _rig(tmp_path)
    rig.dirs.record("sess-a", tmp_path / "deleted")

    await rig.factory.create("sess-a")

    assert _Server.built[0].kwargs["project_dir"] == rig.project
    assert Path(rig.store.get_dir("sess-a")) == rig.project


async def test_a_runtime_has_its_own_queue_router_and_table_and_the_backend_knows_it(
    tmp_path,
) -> None:
    rig = _rig(tmp_path)

    first = await rig.factory.create("sess-a")
    second = await rig.factory.create("sess-b")

    assert first.queue is first.backend.message_queue
    assert first.backend.queue_hook is first.queue
    assert first.queue is not second.queue
    assert first._router is not second._router
    assert first.rpc_table is not second.rpc_table
    assert first.rpc_table
    assert first.backend.runtime is first
    assert isinstance(first.transport, SessionStampingTransport)


async def test_a_server_that_cannot_start_leaves_no_directory_record(tmp_path) -> None:
    rig = _rig(tmp_path)
    _Server.fail_startup = True

    with pytest.raises(RuntimeError, match="cannot start"):
        await rig.factory.create("sess-a")

    assert rig.store.get_dir("sess-a") is None


async def test_the_boot_session_becomes_the_default_on_a_transport_stamping_its_id(
    tmp_path,
) -> None:
    rig = _rig(tmp_path)
    backend = _Server(rig.settings, resume_session_id="boot-1", project_dir=rig.project)
    queue: list[str] = []
    table: dict[str, Any] = {"x": object()}
    router = object()

    runtime = rig.factory.boot(
        BootRuntime(backend=backend, rpc_table=table, router=router, queue=queue)  # type: ignore[arg-type]
    )

    assert runtime.backend is backend
    assert runtime.rpc_table is table
    assert runtime.queue is queue
    assert runtime._router is router
    assert backend.runtime is runtime
    assert isinstance(runtime.transport, SessionStampingTransport)

    from igni_core.protocol import messages as msg

    await runtime.transport.send(msg.Info(text="hi"))
    assert [m.session_id for m in rig.transport.sent] == ["boot-1"]

"""Composition root for the backend process.

:class:`BackendApp` runs the boot in order: listen, load settings, build the
session and its collaborators, print the ready line, then hand everything to
:class:`~igni_core.backend.serving_backend.ServingBackend`, which serves and
owns the teardown order.
"""

from __future__ import annotations

import asyncio
import logging
import os

from igni_core.backend.agent_refresh import AgentRefresh
from igni_core.backend.backend_notifications import BackendNotifications
from igni_core.backend.extension_startup import ExtensionStartup, ExtensionStartupContext
from igni_core.backend.extensions_runtime import ExtensionsRuntime
from igni_core.backend.host_model_runtime import HostModelRuntime
from igni_core.backend.igni_server_access import IgniServerAccess
from igni_core.backend.login_coordinator import LoginCoordinator
from igni_core.backend.observed_transport import ObservedTransport
from igni_core.backend.pool_rpcs import ApproverTokenRpc
from igni_core.backend.pool_wiring import PoolWiring
from igni_core.backend.process_environment import ProcessEnvironment
from igni_core.backend.push_bridge import PushNotificationBridge
from igni_core.backend.rpc_router import RpcRouter
from igni_core.backend.schemas_lifecycle import BackendLaunch
from igni_core.backend.schemas_pool import ProjectDirs
from igni_core.backend.schemas_rpc import LifecyclePhase
from igni_core.backend.server import BackendServer
from igni_core.backend.service_event_ingress import ServiceEventIngress
from igni_core.backend.serving_backend import BackendParts, ServingBackend
from igni_core.backend.session_orchestrator import SessionOrchestrator
from igni_core.backend.session_runtime_factory import BootRuntime
from igni_core.backend.settings_loader import BackendSettingsLoader
from igni_core.backend.supervisor import BackendSupervisor
from igni_core.backend.transport_builder import TransportBuilder
from igni_core.core.config.settings import Settings
from igni_core.core.db.locator import StorageLocator
from igni_core.core.layer import BackendHooks, Layers
from igni_core.core.observability import OtelRuntime
from igni_core.core.plugins.installer import PluginInstaller
from igni_core.core.prompts.contributions import prompt_contributions
from igni_core.extensions.faults import ExtensionFaults
from igni_core.extensions.principal import SignedInPerson
from igni_core.extensions.store import ExtensionStore

logger = logging.getLogger(__name__)


class BackendApp:
    """The backend's composition root: boots, serves, and tears down.

    :attr:`transport` and :attr:`supervisor` stay ``None`` until the boot
    reaches them; they are what a caller watching the boot polls.
    """

    def __init__(self, launch: BackendLaunch) -> None:
        self._launch = launch
        # Cached: the layer builds new hooks on every ask (``core/layer.py``).
        self._guest = Layers.current().backend()
        self._transport_builder = TransportBuilder(
            socket_path=launch.socket_path, ws_port=launch.ws_port, ws_token=launch.ws_token
        )
        self._transport: ObservedTransport | None = None
        self._supervisor: BackendSupervisor | None = None

    @property
    def guest(self) -> BackendHooks:
        """What the deployment layer does around boot and serving."""
        return self._guest

    @property
    def transport(self) -> ObservedTransport | None:
        return self._transport

    @property
    def supervisor(self) -> BackendSupervisor | None:
        return self._supervisor

    @property
    def serving_port(self) -> int | None:
        """The WebSocket port, once the backend serves; ``None`` before and after."""
        ws = self._transport_builder.ws_transport
        supervisor = self._supervisor
        if ws is None or supervisor is None or supervisor.phase is not LifecyclePhase.RUNNING:
            return None
        return ws.port

    def request_shutdown(self) -> None:
        if self._supervisor is not None:
            self._supervisor.request_shutdown()

    async def run(self) -> None:
        """Boot, serve, and tear down. Exceptions in the boot or
        serve phases are logged; teardown always runs."""
        try:
            await self._boot_and_serve()
        finally:
            # Whatever ended the process, the layer tidies what it keeps
            # (the cloud's: ``ready`` and the activity file).
            await self._guest.stop()
            # And what traces are held go to the collector before it ends.
            OtelRuntime.stop()

    async def _boot_and_serve(self) -> None:
        transport = await self._listen()
        settings = BackendSettingsLoader(self._launch.project_dir).load()
        store = ExtensionStore()
        ProcessEnvironment.enter(
            settings,
            tenant=self._guest.tenant,
            person=SignedInPerson(settings.auth.credentials_file, store),
        )
        parts = await self._assemble(transport, settings, store)
        await ServingBackend(parts).serve()

    async def _listen(self) -> ObservedTransport:
        guest = self._guest
        guest.starting()
        # Wrapped once, at the root, so the host object model sees every
        # message igni sends any window (``observed_transport``).
        transport = ObservedTransport(await self._transport_builder.build())
        self._transport = transport
        # Listening, imports done. A layer may wait here for what it needs
        # before igni opens any state (the cloud's: a producer to bind to).
        await guest.listening(transport)
        if await guest.bind(transport):
            # Something was mounted since: a working directory taken
            # before it would still be the old one.
            os.chdir(self._launch.project_dir)
        return transport

    async def _assemble(
        self, transport: ObservedTransport, settings: Settings, store: ExtensionStore
    ) -> BackendParts:
        launch = self._launch
        builder = self._transport_builder
        backend = BackendServer(
            settings,
            project_dir=launch.project_dir,
            resume_session_id=launch.resume_session_id,
            additional_dirs=launch.additional_dirs,
        )
        # Hydrates any persisted ``/loop`` state from the project's ``state.db``.
        await backend.startup()
        loop = asyncio.get_running_loop()
        supervisor = BackendSupervisor.started(
            transport=transport, loop=loop, project_dir=launch.project_dir, backend=backend
        )
        self._supervisor = supervisor
        push_bridge = PushNotificationBridge.wired(transport=transport, loop=loop, backend=backend)
        login = LoginCoordinator(backend=backend, push_bridge=push_bridge)
        plugins_dir = PluginInstaller(data_dir=settings.storage.data_dir).plugins_dir
        extensions = ExtensionsRuntime.build(
            mcp=backend.mcp_manager, plugins_dir=plugins_dir, store=store
        )
        # Read per call: signing in can change which server it is.
        access = IgniServerAccess(
            credentials_file=settings.auth.credentials_file,
            api_url=lambda: backend.settings.api_url,
        )
        notifications = BackendNotifications.build(
            settings=settings,
            access=access,
            push_bridge=push_bridge,
            runtime=extensions,
            mcp=backend.mcp_manager,
            login=login,
        )
        extensions.attach_faults(ExtensionFaults.shared(), notifications.center)
        router = RpcRouter(
            backend=backend,
            login=login,
            push=push_bridge,
            extensions=extensions.rpc,
            origins=extensions.origins,
            notifications=notifications.center,
        )
        rpc_table = router.build_table()

        # A second client opening the same project finds this backend here.
        if ws := builder.ws_transport:
            supervisor.write_discovery_lockfile(ws.port, ws_token=builder.program_token)
        # Before background services: the shell gates on this line, and
        # knowledge load is GIL-heavy enough to look like a hang.
        builder.ready_line().emit()
        backend.start_boot_background_services()
        # Idempotent; the window can retry with the ``start_scheduler`` RPC.
        try:
            push_bridge.start_scheduler(backend)
        except Exception:
            logger.exception("Auto-start of scheduler failed; will retry on client RPC")

        orchestrator = SessionOrchestrator.start(
            boot=BootRuntime(
                backend=backend, rpc_table=rpc_table, router=router, queue=backend.message_queue
            ),
            wiring=PoolWiring(
                transport=transport,
                push_bridge=push_bridge,
                login=login,
                extensions=extensions.rpc,
                origins=extensions.origins,
                notifications=notifications.center,
            ),
            settings=settings,
            dirs=ProjectDirs(
                project_dir=launch.project_dir, additional_dirs=launch.additional_dirs
            ),
        )
        # When what extensions do to the prompts changes, every session's
        # agent is rebuilt, once, on the next turn of the loop.
        prompt_contributions.watch(
            AgentRefresh(loop=loop, refresh=orchestrator.refresh_agents).request
        )
        # The WebSocket's guard mints the tokens igni's window hands a
        # program it lets answer approval requests.
        orchestrator.mount(ApproverTokenRpc(builder.ws_transport))
        pool = orchestrator.pool
        supervisor.set_pool(pool)
        host = HostModelRuntime.build(
            orchestrator=orchestrator,
            transport=transport,
            runtime=extensions,
            project_dir=launch.project_dir,
            store=store,
            notices=notifications.notices,
        )
        ingress = ServiceEventIngress.build(
            producer_db=StorageLocator.from_settings(settings).project_database(launch.project_dir),
            signed_in=access.signed_in_server(),
            orchestrator=orchestrator,
            fetches_own_events=Layers.current().fetches_own_events(),
        )
        orchestrator.mount(ingress.rpc())
        # Background-process narration goes to the boot session only while
        # it is the only one.
        push_bridge.set_live_session_count(lambda: len(pool.runtimes))
        supervisor.start_evictor()
        supervisor.mark_running()
        # Before any window connects: an idle process nobody is looking at
        # is the one a layer that suspends most wants to know about.
        self._guest.serving(
            model=host.model,
            transport=transport,
            orchestrator=orchestrator,
            notifications=notifications.center,
            on_rebind=supervisor.request_shutdown,
            api_url=access.api_url,
        )
        startup = ExtensionStartup(
            runtime=extensions,
            context=ExtensionStartupContext(
                settings=settings,
                project_dir=launch.project_dir,
                plugins_dir=plugins_dir,
                store=store,
                access=access,
                mcp=backend.mcp_manager,
                login=login,
                orchestrator=orchestrator,
                host_channel=host.channel,
            ),
        )
        return BackendParts(
            supervisor=supervisor,
            orchestrator=orchestrator,
            transport=transport,
            no_timeout=launch.ws_port is not None,
            notifications=notifications,
            ingress=ingress,
            extensions=extensions,
            startup=startup,
            host=host,
        )

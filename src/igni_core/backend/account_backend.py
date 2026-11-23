"""The account-only backend: ``python -m igni_core.backend --account-only``.

``docs/design/local-cloud-switch.md`` §6.4. When a person's group runs
igni for them in the cloud, the desktop app chats with their igni there,
through its relay. It still needs four things on this computer, and all
four are igni-core's:

* signing in — the browser hand-off and ``credentials.json``;
* finding the server, and choosing it;
* the group pack, kept fresh, which is how the app learns the mode;
* the status, for the sign-in wall and the account menu.

This backend serves exactly those (:data:`ACCOUNT_RPCS`), with the same
arguments, answers and pushes as a full backend, and keeps the group pack
in step with igni server. It builds no session: no agent, no tools, no MCP
server, no extension, no watcher, no scheduler and no event pull. Its run
gate is closed for good: a chat message is refused with the run gate's
reason, and any other call with ``unavailable``. It writes no discovery
lockfile, so nothing that looks for a project's backend finds this one;
only the window that launched it, holding its per-launch token, reaches it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from igni_core.backend import standing_policy
from igni_core.backend.group_policy_sync import GroupPolicySync
from igni_core.backend.in_flight_tasks import InFlightTasks
from igni_core.backend.login_coordinator import LoginCoordinator
from igni_core.backend.pool_replier import PoolReplier, WindowsTransport
from igni_core.backend.push_bridge import PushNotificationBridge
from igni_core.backend.rpc_binding import NoArgs, RpcBinding, RpcCall
from igni_core.backend.rpc_errors import code_of
from igni_core.backend.rpc_handlers.auth import AuthRpcHandler
from igni_core.backend.rpc_handlers.session import SessionRpcHandler
from igni_core.backend.rpc_result import RpcResult
from igni_core.backend.schemas_auth import DiscoverServerArgs, SetServerArgs
from igni_core.backend.schemas_lifecycle import BackendLaunch
from igni_core.backend.schemas_rpc import LifecyclePhase
from igni_core.backend.server_auth import AuthController
from igni_core.backend.settings_loader import BackendSettingsLoader
from igni_core.backend.supervisor import BackendSupervisor
from igni_core.backend.transport_builder import TransportBuilder
from igni_core.core import air_gap
from igni_core.core.auth.credentials import CloudCredentials
from igni_core.core.auth.discovery import ServerDiscovery
from igni_core.core.auth.portal_client import PortalClient
from igni_core.core.config.group_policy.gate import RunGate
from igni_core.core.config.group_policy.layout import GroupPolicyLayout
from igni_core.core.config.group_policy.meta import PackMetaStore
from igni_core.core.config.settings import Settings
from igni_core.core.config.user_config_store import UserConfigStore
from igni_core.core.session.cloud_auth import SessionCloudAuth
from igni_core.core.session.cloud_catalog import CloudModelCatalog
from igni_core.protocol import messages as msg
from igni_core.protocol.messages import Message
from igni_core.protocol.rpc import RpcMethod
from igni_core.protocol.schemas.rpc import RpcErrorCode
from igni_core.transport.base import ListeningTransport

logger = logging.getLogger(__name__)

#: What the account-only backend answers: signing in and out, the server,
#: the plan and the status. Everything else is ``unavailable``.
ACCOUNT_RPCS: frozenset[RpcMethod] = frozenset(
    {
        RpcMethod.LOGIN,
        RpcMethod.RELOAD_CLOUD_CREDENTIALS,
        RpcMethod.CLEAR_CLOUD_CREDENTIALS,
        RpcMethod.GET_CLOUD_PLAN,
        RpcMethod.DISCOVER_SERVER,
        RpcMethod.SET_SERVER,
        RpcMethod.GET_STATUS,
    }
)


class AccountHolder:
    """Who is signed in, for a backend that holds no session.

    What :class:`~igni_core.backend.server_auth.AuthController` and
    :class:`~igni_core.backend.group_policy_sync.GroupPolicySync` change on
    a sign-in: the credentials and the settings. There is no agent to
    rebuild and no group agent to load.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cloud_auth = SessionCloudAuth(
            creds=CloudCredentials(settings.auth.credentials_file),
            server_url=settings.api_url,
            rebuild_team=lambda: None,
            catalog=CloudModelCatalog(settings),
        )

    @property
    def cloud_auth(self) -> SessionCloudAuth:
        return self._cloud_auth

    @property
    def settings(self) -> Settings:
        return self._settings

    def rebuild_main_team(self) -> None:
        """No agent here."""

    def reload_group_agents(self) -> bool:
        return False


@dataclass(frozen=True)
class AccountControllers:
    """The one controller the account-only backend has."""

    auth: AuthController


@dataclass(frozen=True)
class AccountServer:
    """What the login coordinator signs in through."""

    controllers: AccountControllers


class AccountStatus:
    """The ``status_update`` the sign-in wall and the account menu read.

    The account half of a full backend's: who is signed in, to which
    organisation, and the group's brand colours. No model is in use here.
    """

    def __init__(self, holder: AccountHolder, pack_meta: PackMetaStore) -> None:
        self._holder = holder
        self._pack_meta = pack_meta

    def __call__(self) -> msg.StatusUpdate:
        cloud_auth = self._holder.cloud_auth
        meta = self._pack_meta.load()
        theme = meta.theme if meta is not None else None
        return msg.StatusUpdate(
            cloud_connected=cloud_auth.connected,
            cloud_org=cloud_auth.org_name or "",
            cloud_email=cloud_auth.email or "",
            theme=(theme.model_dump() or None) if theme is not None else None,
        )


class AccountRpcs:
    """The account-only backend's dispatch table.

    Each call validates its arguments with the binding the full backend's
    handler declares for it, and answers through the same controller, so
    the account backend's contract is the same one
    (``docs/rpc-contract.json``), not a copy.
    """

    def __init__(
        self,
        *,
        auth: AuthController,
        login: LoginCoordinator,
        status: Callable[[], msg.StatusUpdate],
    ) -> None:
        self._auth = auth
        self._login = login
        self._status = status

    def table(self) -> dict[str, RpcCall]:
        implementations: dict[RpcMethod, Callable[[Any], Any]] = {
            RpcMethod.LOGIN: self._sign_in,
            RpcMethod.RELOAD_CLOUD_CREDENTIALS: self._reload,
            RpcMethod.CLEAR_CLOUD_CREDENTIALS: self._sign_out,
            RpcMethod.GET_CLOUD_PLAN: self._plan,
            RpcMethod.DISCOVER_SERVER: self._discover,
            RpcMethod.SET_SERVER: self._set_server,
            RpcMethod.GET_STATUS: self._get_status,
        }
        bindings = {**RpcBinding.on(AuthRpcHandler), **RpcBinding.on(SessionRpcHandler)}
        return {
            method: bindings[method][1].call(implementations[method]) for method in ACCOUNT_RPCS
        }

    async def _sign_in(self, args: NoArgs) -> Any:
        return await self._login.start()

    def _reload(self, args: NoArgs) -> msg.StatusUpdate:
        return self._auth.reload_cloud_credentials()

    def _sign_out(self, args: NoArgs) -> msg.StatusUpdate:
        return self._auth.clear_cloud_credentials()

    async def _plan(self, args: NoArgs) -> Any:
        return await self._auth.get_cloud_plan()

    async def _discover(self, args: DiscoverServerArgs) -> Any:
        return await self._auth.discover_server(args.email)

    def _set_server(self, args: SetServerArgs) -> Any:
        return self._auth.set_server(args.api_url, args.portal_url)

    def _get_status(self, args: NoArgs) -> msg.StatusUpdate:
        return self._status()


class AccountDispatcher:
    """Answers each message a window sends the account-only backend."""

    def __init__(
        self,
        *,
        table: dict[str, RpcCall],
        login: LoginCoordinator,
        gate: RunGate,
        transport: WindowsTransport,
    ) -> None:
        self._table = table
        self._login = login
        self._gate = gate
        self._transport = transport
        self._replier = PoolReplier(transport)

    async def dispatch(self, message: Message) -> None:
        """Answer ``message``. A failure is the caller's answer, never a crash."""
        refusal = standing_policy.refusal(message)
        if refusal is not None:
            await self._replier.refuse(message, refusal)
            return
        if isinstance(message, msg.RPCRequest):
            await self._call(message)
            return
        if isinstance(message, msg.CancelLogin):
            await self._login.cancel()
            return
        # A chat message, a command, an approval, a cancel: nothing runs here.
        await self._replier.refuse(message, self._gate.reason or RunGate.IN_THE_CLOUD)

    async def _call(self, request: msg.RPCRequest) -> None:
        call = self._table.get(request.method)
        if request.caller or call is None:
            await self._reply(
                request,
                msg.RPCResponse.fail(
                    request.id,
                    f"{request.method}: this igni only signs you in "
                    f"({self._gate.reason or RunGate.IN_THE_CLOUD})",
                    RpcErrorCode.UNAVAILABLE,
                ),
            )
            return
        try:
            result = call(request.args)
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as exc:
            logger.info("account rpc %s failed: %s", request.method, exc)
            await self._reply(request, msg.RPCResponse.fail(request.id, str(exc), code_of(exc)))
            return
        if isinstance(result, Message):
            # ``status_update`` is sent in place of an ``rpc_response``, as a
            # full backend sends it.
            await self._reply(request, result.model_copy(update={"id": request.id}))
            return
        await self._reply(request, msg.RPCResponse.ok(request.id, RpcResult.serialize(result)))

    async def _reply(self, request: msg.RPCRequest, response: Message) -> None:
        await self._transport.send(response.address_to(request.from_client))


class AccountBackend:
    """The account-only backend's composition root: boots, serves, tears down."""

    def __init__(self, launch: BackendLaunch) -> None:
        self._launch = launch
        self._transport_builder = TransportBuilder(
            socket_path=launch.socket_path, ws_port=launch.ws_port, ws_token=launch.ws_token
        )
        self._transport: ListeningTransport | None = None
        self._supervisor: BackendSupervisor | None = None
        self._gate = RunGate()
        self._tasks = InFlightTasks()

    @property
    def gate(self) -> RunGate:
        """Closed for the life of the process."""
        return self._gate

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
        transport = await self._transport_builder.build()
        self._transport = transport
        loop = asyncio.get_running_loop()
        # No session to fall back on: the supervisor watches the signals,
        # the parent and the transport, and closes the transport at the end.
        supervisor = BackendSupervisor.started(
            transport=transport, loop=loop, project_dir=self._launch.project_dir, backend=None
        )
        self._supervisor = supervisor
        sync: GroupPolicySync | None = None
        login: LoginCoordinator | None = None
        try:
            settings = BackendSettingsLoader(
                self._launch.project_dir, self._gate, self._launch.cli_overrides
            ).load()
            # Whatever the pack says: this process runs nothing.
            self._gate.close(self._gate.reason or RunGate.IN_THE_CLOUD)
            # Sign-in reaches igni server: an air-gapped install keeps to it.
            air_gap.export_to(os.environ, settings.air_gapped)
            holder = AccountHolder(settings)
            portal = PortalClient(api_url=settings.api_url, portal_url=settings.portal_url or None)
            sync = GroupPolicySync(holder, settings, portal)
            status = AccountStatus(
                holder, PackMetaStore(GroupPolicyLayout.for_data_dir(settings.storage.data_dir))
            )
            auth = AuthController(
                holder,
                settings,
                status,
                portal=portal,
                group_policy=sync,
                user_config_store=UserConfigStore(),
                server_discovery=ServerDiscovery(),
            )
            push = PushNotificationBridge(transport=transport, loop=loop, queue=[])
            login = LoginCoordinator(
                backend=AccountServer(AccountControllers(auth=auth)), push_bridge=push
            )
            dispatcher = AccountDispatcher(
                table=AccountRpcs(auth=auth, login=login, status=status).table(),
                login=login,
                gate=self._gate,
                transport=transport,
            )
            self._transport_builder.ready_line().emit()
            supervisor.mark_running()
            sync.start()
            logger.info("account-only backend serving: sign-in, server, plan and status")
            await self._serve(transport, dispatcher, supervisor.shutdown_event)
        except Exception as exc:
            logger.error("Account backend error: %s", exc, exc_info=True)
        finally:
            if sync is not None:
                sync.stop_polling()
            if login is not None:
                with contextlib.suppress(Exception):
                    await login.cancel()
            await supervisor.teardown()

    async def _serve(
        self,
        transport: ListeningTransport,
        dispatcher: AccountDispatcher,
        shutdown: asyncio.Event,
    ) -> None:
        """Read from the window until shutdown, each message its own task."""
        incoming = aiter(transport.receive())
        stopping = asyncio.ensure_future(shutdown.wait())
        try:
            while True:
                arriving = asyncio.ensure_future(anext(incoming))
                await asyncio.wait({arriving, stopping}, return_when=asyncio.FIRST_COMPLETED)
                if stopping.done():
                    arriving.cancel()
                    break
                try:
                    message = arriving.result()
                except StopAsyncIteration:
                    break
                if isinstance(message, msg.Shutdown) and standing_policy.refusal(message) is None:
                    break
                self._tasks.spawn(dispatcher.dispatch(message), name="account-dispatch")
        finally:
            stopping.cancel()
        for task in self._tasks.pending():
            task.cancel()
        await self._tasks.drain()


__all__ = [
    "ACCOUNT_RPCS",
    "AccountBackend",
    "AccountControllers",
    "AccountDispatcher",
    "AccountHolder",
    "AccountRpcs",
    "AccountServer",
    "AccountStatus",
]

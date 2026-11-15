"""Extensions in a headless run (``-m``, ``--pipe``).

An extension's backend logic runs in every mode; only its UI needs an
app to load into. A headless run is one session, one message, no
window — so it starts the extensions installed on this machine the way
the app does (:meth:`~igni_core.backend.extension_startup.ExtensionStartup.start`,
without the distribution sync), and their tools, hooks and prompt
contributions apply to it. Without this a policy extension's
``PreToolUse`` hook could be stepped around by running igni with ``-m``.

The host model is there for the processes to connect to — it carries
the hook events — but there is no window behind it: a member that would
act on one is refused, and says why.

Starting is bounded (:attr:`HeadlessExtensions.START_TIMEOUT_S`). An
extension that did not come up in time is ``FAILED``, and a fail-closed
hook it holds (``PreToolUse``, ``UserPromptSubmit``) refuses its events
with that reason rather than letting them through unchecked.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from igni_core.backend.activity import Activity
from igni_core.backend.command_handler import CommandHandler
from igni_core.core.config.settings import Settings
from igni_core.core.hooks.extension_gate import extension_hook_gate
from igni_core.core.plugins.installer import PluginInstaller
from igni_core.core.session.client_state import ClientStateRead, ClientStateResult
from igni_core.core.session.event_log_schema import VisualizationDeltaPayload
from igni_core.core.session.schemas import PersistResult
from igni_core.extensions.contributions import ExtensionContributions
from igni_core.extensions.distribution.once import OneShotSync
from igni_core.extensions.host.attachments import ChatAttachments
from igni_core.extensions.host.broadcasts.views import ViewShown
from igni_core.extensions.host.channel import HostChannel
from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.hooks import ExtensionHooks
from igni_core.extensions.host.model import HostModel
from igni_core.extensions.host.model_config import HostModelConfig
from igni_core.extensions.host.processes import ExtensionProcesses
from igni_core.extensions.host.spec import ErrorCode
from igni_core.extensions.host.values import SessionAttached
from igni_core.extensions.impact import IgniCommands
from igni_core.extensions.installer import ExtensionInstaller
from igni_core.extensions.launcher import ExtensionLauncher
from igni_core.extensions.origin import ExtensionOrigins
from igni_core.extensions.principal import SignedInPerson
from igni_core.extensions.profile import DeploymentProfile, DevelopmentFlag
from igni_core.extensions.python_env import PythonEnvironments
from igni_core.extensions.registry import ExtensionRegistry, ExtensionState
from igni_core.extensions.rpc import ExtensionRpc
from igni_core.extensions.service_events import UnattendedPost
from igni_core.extensions.service_token import (
    CredentialsSession,
    ServiceTokenEndpoint,
    ServiceTokenExchange,
)
from igni_core.extensions.store import ExtensionStore
from igni_core.protocol.messages import Message
from igni_core.protocol.rpc import RpcMethod
from igni_core.transport.ws_auth import frame_ancestor_sources

if TYPE_CHECKING:
    from igni_core.extensions.sandbox import ExtensionSandbox

logger = logging.getLogger(__name__)


class RunState:
    """What windows remember, in a run that has none: a scratchpad.

    A headless run has no window, so nothing reads this state back
    after the run ends — but it is kept for the length of the run, so
    that a write which worked is a write which worked, exactly as in
    the database store this one stands in for. The store answers with
    a result either way, and its ``ok`` means one thing in both.
    """

    def __init__(self) -> None:
        self._values: dict[tuple[str, str], str] = {}

    def get_for_client(self, client_id: str) -> ClientStateRead:
        return ClientStateRead(
            ok=True,
            values={
                key: value for (client, key), value in self._values.items() if client == client_id
            },
        )

    def set_value(self, client_id: str, key: str, value: str) -> ClientStateResult:
        self._values[(client_id, key)] = value
        return ClientStateResult(ok=True)

    def delete_value(self, client_id: str, key: str) -> ClientStateResult:
        self._values.pop((client_id, key), None)
        return ClientStateResult(ok=True)


class HeadlessHostBackend:
    """The host model's backend in a run with no window: nothing to act on."""

    def __init__(self, project_dir: Path, session_id: str) -> None:
        self.project_dir = project_dir
        self.client_state = RunState()
        self._session_id = session_id

    @staticmethod
    def _refused(what: str) -> HostModelError:
        return HostModelError(ErrorCode.REFUSED, f"{what}: this is a headless run, with no window")

    def default_session(self) -> str:
        return self._session_id

    def live_sessions(self) -> list[str]:
        return [self._session_id]

    async def rpc(self, method: RpcMethod, args: dict[str, Any], *, session_id: str) -> Any:
        raise self._refused(method.value)

    async def act(self, message: Message, *, session_id: str) -> None:
        raise self._refused(type(message).__name__)

    async def ask(self, message: Message, *, session_id: str, timeout: float = 120.0) -> Message:
        raise self._refused(type(message).__name__)

    async def attach(self, session_id: str, project_dir: str | None) -> SessionAttached:
        raise self._refused("attach_session")

    async def post(
        self,
        *,
        session_id: str,
        extension_id: str,
        text: str,
        media: ChatAttachments,
        queued: bool,
    ) -> None:
        raise self._refused("chat.post")

    async def deliver(
        self,
        *,
        session_id: str,
        extension_id: str,
        text: str,
        media: ChatAttachments,
    ) -> UnattendedPost:
        raise self._refused("chat.post")

    async def title(self, *, session_id: str, title: str) -> PersistResult:
        raise self._refused("sessions.create")

    async def show_view(
        self, *, session_id: str, payload: VisualizationDeltaPayload, run_id: str
    ) -> None:
        raise self._refused("chat.show")

    def views(self, session_id: str) -> list[ViewShown]:
        return []

    def activity(self) -> Activity:
        # A headless run is its own process; nothing here suspends it.
        return Activity.of([])

    def waiting_approvals(self, session_id: str) -> list[Message]:
        # Headless runs ask nobody: nothing waits on a person.
        return []

    def project_dir_of(self, session_id: str) -> Path:
        return self.project_dir

    def broadcast(self, message: Message) -> None:
        return None


class HeadlessExtensions:
    """Starts this machine's installed extensions for one headless run, and stops them."""

    #: How long the run waits for its extensions to come up.
    START_TIMEOUT_S = 60.0
    #: How long it waits for igni server's listing and downloads first.
    SYNC_TIMEOUT_S = 30.0

    def __init__(self, session: Any) -> None:
        self._session = session
        self._registry = ExtensionRegistry()
        self._channel: HostChannel | None = None
        self._contributions: ExtensionContributions | None = None
        self._plugins_dir: Path | None = None

    @property
    def registry(self) -> ExtensionRegistry:
        return self._registry

    async def start(self) -> None:
        """Start what is installed, bounded; then rebuild the agents with what they add."""
        session = self._session
        settings = session.settings
        store = ExtensionStore()
        if not store.installed():
            # Nothing stored, so nothing an upgrade could need to put back.
            await self._sync(store, settings)
            if not store.installed():
                return
            launcher = self._launcher(store)
        else:
            # The sync starts each upgrade it stores, through the run's own
            # launcher, so one that does not start puts the working version
            # back (sync.py ``_install``). Without it the sync deleted the
            # version that worked and kept one that could not start.
            launcher = self._launcher(store)
            await self._sync(store, settings, lifecycle=launcher)
        plugins_before = self._plugin_dirs()
        try:
            await asyncio.wait_for(
                launcher.start_installed(skip=set(self._registry.ids())), self.START_TIMEOUT_S
            )
        except TimeoutError:
            for extension_id in self._registry.ids():
                entry = self._registry.get(extension_id)
                if entry is not None and entry.state is ExtensionState.PREPARING:
                    self._registry.set_state(
                        extension_id,
                        ExtensionState.FAILED,
                        f"it did not start within {self.START_TIMEOUT_S:g}s",
                    )
        for extension_id in self._registry.ids():
            entry = self._registry.get(extension_id)
            if entry is not None and entry.state is not ExtensionState.READY:
                logger.warning("extension %s is %s: %s", extension_id, entry.state, entry.reason)
        # The one turn's prompt is built by the rebuild below: what the
        # extensions contribute to it has to be in by then.
        await self._contributions_settled()
        if self._plugin_dirs() != plugins_before:
            # An extension's plugin is copied in as the extension starts —
            # after this run's skills, hooks and agents were loaded. Without
            # a reload the first run after an install had none of them
            # ("Unknown command"), and only the next run did.
            session.reload_plugins()
        session.reload_group_agents()

    def _plugin_dirs(self) -> set[str]:
        """The plugins installed now, by directory name."""
        plugins_dir = self._plugins_dir
        if plugins_dir is None or not plugins_dir.is_dir():
            return set()
        return {child.name for child in plugins_dir.iterdir() if child.is_dir()}

    async def _contributions_settled(self) -> None:
        if self._contributions is not None:
            await self._contributions.settled(self.START_TIMEOUT_S)

    def _launcher(self, store: ExtensionStore) -> ExtensionLauncher:
        """The run's host channel, hooks and installer, and the launcher over them."""
        session = self._session
        settings = session.settings
        rpc = ExtensionRpc(self._registry)
        plugins_dir = PluginInstaller(data_dir=settings.storage.data_dir).plugins_dir
        self._plugins_dir = Path(plugins_dir)
        hooks = ExtensionHooks(registry=self._registry)
        contributions = ExtensionContributions(
            registry=self._registry,
            rpc=rpc,
            plugins_dir=plugins_dir,
            priority=store.listing.priority,
            igni_commands=IgniCommands.of(CommandHandler.builtin_names()),
            prompt_loader=session.prompt_loader,
            prompts=session.prompt_contributions,
            hooks=hooks,
        )
        contributions.attach()
        self._contributions = contributions
        model = HostModel.build(
            HostModelConfig(
                backend=HeadlessHostBackend(Path(session.project_dir), str(session.session_id)),
                registry=self._registry,
                store=store,
            )
        )
        self._channel = HostChannel(model, hooks=hooks, processes=ExtensionProcesses())
        extension_hook_gate.install(hooks)
        person = SignedInPerson(settings.auth.credentials_file, store)
        session.mcp_manager.extension_request_meta = person.meta
        hooks.set_principal(person)
        exchange = ServiceTokenExchange(
            api_url=lambda: settings.api_url,
            session=CredentialsSession(settings.auth.credentials_file),
        )
        installer = ExtensionInstaller(
            registry=self._registry,
            rpc=rpc,
            mcp=session.mcp_manager,
            origins=ExtensionOrigins(frame_ancestors=frame_ancestor_sources()),
            project_dir=Path(session.project_dir),
            profile=DeploymentProfile.current(),
            plugins_dir=plugins_dir,
            allow_development=DevelopmentFlag.enabled(),
            store=store,
            environments=PythonEnvironments(store.environments_root),
            service_tokens=ServiceTokenEndpoint(exchange, principal=person),
            host_channel=self._channel,
            # Its air gap narrows what an extension's process may reach.
            sandbox=self._sandbox(settings),
        )
        return ExtensionLauncher(
            store=store, installer=installer, registry=self._registry, contributions=contributions
        )

    async def _sync(
        self,
        store: ExtensionStore,
        settings: Settings,
        *,
        lifecycle: ExtensionLauncher | None = None,
    ) -> None:
        """Bring the store in step with igni server first, bounded.

        The app's backend syncs once a window attaches; a headless run
        has none, so without this it never received an extension its
        organisation switched on. Skipped — with the reason in the log —
        when there is no server, nobody is signed in or the air gap
        excludes it; a failure costs the update, never the run.
        """
        report = await OneShotSync(store, settings, lifecycle=lifecycle).run(
            timeout=self.SYNC_TIMEOUT_S
        )
        if report.skipped:
            logger.info("extension sync skipped: %s", report.skipped)
        elif report.installed or report.removed or report.failed:
            logger.info(
                "extension sync: installed %s, removed %s, failed %s",
                report.installed,
                report.removed,
                report.failed,
            )

    @staticmethod
    def _sandbox(settings: Settings) -> ExtensionSandbox:
        """The OS sandbox every extension process here starts in.

        Imported here rather than at the top: this module is reached
        from ``extensions.host.channel`` through the session runner, and
        a top-level import of the sandbox package closes that cycle.
        """
        from igni_core.extensions.sandbox import ExtensionSandbox

        return ExtensionSandbox.for_settings(settings)

    async def stop(self) -> None:
        """Stop the extensions' processes, and take their hooks out of the gate."""
        if self._channel is None:
            return
        extension_hook_gate.install(None)
        await self._session.mcp_manager.disconnect_all()
        await self._channel.stop()


__all__ = ["HeadlessExtensions", "HeadlessHostBackend", "RunState"]

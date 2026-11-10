"""Start what is installed, then bring it up to date.

Two sources, one boot path. What is already in the store (extensions igni
distributed earlier, and development installs ``igni ext install`` put
there) starts first, from disk, with no network. Then, in the background,
the distribution sync asks igni server which extensions its approved
services ship and installs, upgrades or removes to match (igni server's
``docs/service-identity.md`` §8). Start-up never waits for that: a machine
that cannot reach igni runs what it has.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, SkipValidation

from igni_core.backend.extensions_runtime import ExtensionsRuntime
from igni_core.backend.igni_server_access import IgniServerAccess
from igni_core.backend.login_coordinator import LoginCoordinator
from igni_core.backend.pool_rpcs import PageTokenRpc
from igni_core.backend.session_orchestrator import SessionOrchestrator
from igni_core.core.config.settings import Settings
from igni_core.extensions.distribution.sync import ExtensionDistribution
from igni_core.extensions.host.channel import HostChannel
from igni_core.extensions.installer import ExtensionInstaller
from igni_core.extensions.launcher import ExtensionLauncher
from igni_core.extensions.principal import SignedInPerson
from igni_core.extensions.profile import DeploymentProfile, DevelopmentFlag
from igni_core.extensions.python_env import PythonEnvironments
from igni_core.extensions.sandbox import ExtensionSandbox
from igni_core.extensions.service_token import PageServiceTokens, ServiceTokenEndpoint
from igni_core.extensions.store import ExtensionStore


class ExtensionStartupContext(BaseModel):
    """What starting the extensions needs from the rest of the backend.

    The collaborators a test stands in for are not validated.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    settings: Settings
    project_dir: Path
    #: Where a plugin an extension carries goes: the directory
    #: ``igni plugin install`` writes to, because the loader looks in one place.
    plugins_dir: Path
    store: ExtensionStore
    access: IgniServerAccess
    #: The MCP client manager an extension's service connects through.
    mcp: Annotated[Any, SkipValidation]
    login: Annotated[LoginCoordinator, SkipValidation]
    orchestrator: Annotated[SessionOrchestrator, SkipValidation]
    #: ``None`` where no host model is built.
    host_channel: Annotated[HostChannel | None, SkipValidation] = None


class ExtensionStartup:
    """Starts the installed extensions and schedules the distribution sync.

    Failures are logged and skipped: one extension that will not start must
    not stop the backend, and the registry records why so the UI can say it.
    """

    def __init__(self, *, runtime: ExtensionsRuntime, context: ExtensionStartupContext) -> None:
        self._runtime = runtime
        self._context = context
        self._service_tokens: ServiceTokenEndpoint | None = None
        self._distribution: ExtensionDistribution | None = None

    @property
    def service_tokens(self) -> ServiceTokenEndpoint | None:
        return self._service_tokens

    @property
    def distribution(self) -> ExtensionDistribution | None:
        return self._distribution

    async def start(self) -> None:
        context = self._context
        runtime = self._runtime
        store = context.store
        # Whom every call into an extension is for: on this backend, the
        # person signed in here (docs/EXTENSION_ROUTING_AND_CONCURRENCY.md).
        person = SignedInPerson(context.settings.auth.credentials_file, store)
        context.mcp.extension_request_meta = person.meta
        runtime.hooks.set_principal(person)
        # One exchange, one cache: an extension's process and its page ask
        # for the same token and igni is asked once.
        exchange = context.access.token_exchange()
        self._service_tokens = ServiceTokenEndpoint(exchange, principal=person)
        context.orchestrator.mount(
            PageTokenRpc(PageServiceTokens(origins=runtime.origins, exchange=exchange))
        )
        installer = ExtensionInstaller(
            registry=runtime.registry,
            rpc=runtime.rpc,
            mcp=context.mcp,
            origins=runtime.origins,
            project_dir=context.project_dir,
            profile=DeploymentProfile.current(),
            plugins_dir=context.plugins_dir,
            allow_development=DevelopmentFlag.enabled(),
            store=store,
            environments=PythonEnvironments(store.environments_root),
            service_tokens=self._service_tokens,
            host_channel=context.host_channel,
            # Its air gap narrows what an extension's process may reach.
            sandbox=ExtensionSandbox.for_settings(context.settings),
        )
        launcher = ExtensionLauncher(
            store=store,
            installer=installer,
            registry=runtime.registry,
            contributions=runtime.contributions,
        )
        await launcher.start_installed()

        self._distribution = ExtensionDistribution(
            store,
            api_url=context.access.api_url,
            session=context.access.session,
            lifecycle=launcher,
            on_priority=runtime.contributions.priority_changed,
        )
        self._distribution.schedule()
        # And again whenever somebody signs in: the listing is the signed-in
        # person's, and before that there was none.
        context.login.on_signed_in(self._distribution.schedule)

    async def stop(self) -> None:
        if self._distribution is not None:
            await self._distribution.stop()
        if self._service_tokens is not None:
            await self._service_tokens.stop()

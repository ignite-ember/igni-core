"""What an extension's process is told, and what it may keep.

Two things live here, and they are the same thing seen from two sides:
the **environment** a process igni starts is given, and the **scope**
that environment carries — the grants, the directories and the tokens
that name one extension.

The environment is a typed model rather than a bag of strings because
its parts come from different places and have a precedence that
matters. A bundle that shipped ``IGNI_SERVICE_TOKEN_URL`` itself must
not get one by shipping it, and a deployment that resolved a secret
must not be overridden by an environment variable the bundle chose.
So the parts stay apart — what the bundle shipped, what igni grants,
the two directories — and are merged in one place, in one order.

A process igni starts that belongs to a service is told how to get a
token for it — ``IGNI_SERVICE_URL``, ``IGNI_SERVICE_TOKEN_URL`` and
``IGNI_SERVICE_TOKEN_SECRET`` (:mod:`igni_core.extensions.service_token`).
It never sees the person's igni session. It is also given a directory
of its own, ``IGNI_EXTENSION_DATA``: what it keeps on this machine —
a database, a model — it runs and stores itself.
igni provides no databases or models to extensions. What it runs that
is not Python ships with it as assets, checked against the digests its
signed manifest pins and laid out read-only under ``<bundle>/assets/``,
which ``IGNI_EXTENSION_ASSETS`` names (:mod:`igni_core.extensions.assets`).
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from igni_core.core.agno_telemetry import AgnoTelemetry
from igni_core.extensions.assets import ASSETS_DIR_ENV
from igni_core.extensions.install_context import InstallContext
from igni_core.extensions.permissions import Permissions
from igni_core.extensions.service_token import ServiceTokenEndpoint
from igni_core.extensions.store import DATA_DIR, DATA_DIR_ENV, DistributionRecord, ExtensionStore
from igni_core.extensions.web_home import SERVICE_URL_SETTING

if TYPE_CHECKING:
    from igni_core.extensions.host.channel import HostChannel
    from igni_core.extensions.manifest import ExtensionManifest

logger = logging.getLogger(__name__)


class ProcessEnvironment(BaseModel):
    """The environment one extension's process starts with.

    The parts are kept apart because they come from different places
    and have a different precedence; :meth:`merged` is the only place
    that order is written down.
    """

    #: What the bundle shipped for itself. Never above a grant.
    literals: dict[str, str] = Field(default_factory=dict)
    #: What the extension's own grants say: its workspace, its secrets.
    service: dict[str, str] = Field(default_factory=dict)
    #: What igni tells it about igni: the host channel, the token
    #: endpoint for its service.
    grants: dict[str, str] = Field(default_factory=dict)
    #: Where the assets it ships are laid out on this machine.
    assets_dir: Path | None = None
    #: The directory that is this extension's alone on this machine.
    data_dir: Path | None = None

    def with_literals(self, literals: dict[str, str]) -> ProcessEnvironment:
        """The same environment, with the bundle's own values in it."""
        return self.model_copy(update={"literals": dict(literals)})

    def merged(self) -> dict[str, str]:
        """The environment, in precedence order.

        The manifest's own literals first, so nothing igni grants can
        be shadowed by a value the bundle shipped — an extension
        setting ``IGNI_WORKSPACE`` or ``IGNI_SERVICE_TOKEN_URL`` itself
        must not be how it gets one. Then the grants, then the host,
        the token endpoint, the assets and the data directory. Last,
        Agno's telemetry switch: an MCP stdio launch inherits almost
        nothing of igni's environment, so an extension built on Agno
        would otherwise run with Agno's default, which posts every run
        to os-api.agno.com (``core/agno_telemetry.py``). Last, so a
        bundle cannot turn it on.
        """
        env = dict(self.literals)
        env.update(self.service)
        env.update(self.grants)
        if self.assets_dir is not None:
            env[ASSETS_DIR_ENV] = str(self.assets_dir)
        if self.data_dir is not None:
            env[DATA_DIR_ENV] = str(self.data_dir)
        env.update(AgnoTelemetry.child_environment())
        return env


class ExtensionEnvironment:
    """The scope one extension is given, and the environment it starts with."""

    def __init__(
        self,
        *,
        context: InstallContext,
        service_tokens: ServiceTokenEndpoint | None = None,
        host_channel: HostChannel | None = None,
        store: ExtensionStore | None = None,
    ) -> None:
        """
        Args:
            context: The deployment this install happens in.
            service_tokens: The loopback endpoint an extension fetches
                its service's token from. Without one, no extension is
                told how to reach its service as the signed-in person.
            host_channel: The loopback socket an extension's process
                reaches the host object model on (``ext.host``). Without
                one, a process is not told where igni is.
            store: Where a development install's settings are read, for
                the ``serviceUrl`` its token is fetched for.
        """
        self._context = context
        self._service_tokens = service_tokens
        self._host_channel = host_channel
        self._store = store

    def permissions(self, manifest: ExtensionManifest) -> Permissions:
        """What this extension is allowed to do, decided from its manifest."""
        return Permissions(
            manifest,
            secret_resolver=self._context.secret_resolver,
            development=self._context.allow_development,
        )

    def service_url(
        self, manifest: ExtensionManifest, distribution: DistributionRecord | None
    ) -> str | None:
        """The service this extension belongs to, if it has one.

        From the distribution record — igni says which service shipped
        it — or, for a development install, the extension's own
        ``serviceUrl`` setting, because there is no record to ask.
        """
        if distribution is not None:
            return distribution.service_url
        if self._store is None:
            return None
        value = self._store.settings(manifest).get(SERVICE_URL_SETTING)
        text = str(value).strip() if value is not None else ""
        return text or None

    async def build(
        self,
        manifest: ExtensionManifest,
        permissions: Permissions,
        service_url: str | None,
        assets_dir: Path | None = None,
    ) -> ProcessEnvironment:
        """The environment for this extension's process.

        Only for a process igni starts. One secret per start, shared by
        the two loopback endpoints, so one value names one process
        wherever it presents it: the host model's channel
        (``IGNI_HOST_URL``, ``IGNI_HOST_SECRET``) and, when the extension
        belongs to a service, the token endpoint for it
        (``IGNI_SERVICE_TOKEN_URL``, ``IGNI_SERVICE_TOKEN_SECRET``).
        """
        grants: dict[str, str] = {}
        data_dir: Path | None = None
        if manifest.runs_here:
            secret = secrets.token_urlsafe(32)
            if self._service_tokens is not None and service_url is not None:
                grants.update(
                    await self._service_tokens.grant(manifest.id, service_url, secret=secret)
                )
            if self._host_channel is not None:
                grants.update(await self._host_channel.grant(manifest.id, secret))
            data_dir = self.data_dir(manifest.id)
        return ProcessEnvironment(
            service=permissions.service_environment(self._context.project_dir),
            grants=grants,
            assets_dir=assets_dir,
            data_dir=data_dir,
        )

    def data_dir(self, extension_id: str) -> Path:
        """The directory that is this extension's alone on this machine.

        Beside the store rather than inside the bundle, so it survives
        an upgrade or a reinstall: what an extension keeps here (a
        graph per commit, a model it downloaded) is expensive to make
        again. Made here, so the process can rely on it existing.
        """
        root = self._store.root if self._store is not None else ExtensionStore().root
        path = root / DATA_DIR / extension_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def revoke(self, extension_id: str) -> None:
        """Take back the tokens this extension was given."""
        if self._service_tokens is not None:
            self._service_tokens.revoke(extension_id)
        if self._host_channel is not None:
            self._host_channel.revoke(extension_id)

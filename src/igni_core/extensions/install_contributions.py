"""What an extension adds to this deployment: its pages and its plugin.

Two contributions, and both of them are optional. The pages are served
on an origin of their own, because a port is part of an origin and a
path prefix is not — two extensions sharing one origin could read each
other's storage and script each other's frames. The plugin is copied
into the plugins directory under the extension's own id, so that
uninstalling finds it again and a plugin free to call itself anything
is a plugin that can be left behind.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from igni_core.core.agents.tool_audience import ToolAudience
from igni_core.core.agents.tool_audience import audience as default_audience
from igni_core.extensions.install_context import InstallContext
from igni_core.extensions.install_service import server_name
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.origin import ExtensionOrigins
from igni_core.extensions.permissions import Permissions, ServiceOrigin

logger = logging.getLogger(__name__)


class ExtensionPages:
    """An extension's pages, on an origin of their own."""

    def __init__(self, *, origins: ExtensionOrigins | None = None, context: InstallContext) -> None:
        """
        Args:
            origins: Where an extension's UI is served from. Optional:
                an extension that contributes no pages needs none, and
                neither does a headless deployment.
            context: The deployment this install happens in.
        """
        self._origins = origins
        self._context = context

    async def serve(
        self,
        manifest: ExtensionManifest,
        bundle: Path,
        permissions: Permissions,
        service_url: str | None,
    ) -> str | None:
        """Put this extension's pages on their own origin.

        Its CSP's ``connect-src`` is the page itself, its own service
        and its declared egress — nothing else
        (:meth:`~igni_core.extensions.permissions.Egress.csp_connect_src`).
        The service is recorded on the origin too, as the one audience
        the page's ``bridge.service`` token is fetched for.

        Returns a reason on failure, or ``None`` when there was
        nothing to serve or it was served.
        """
        if not manifest.contributes.pages:
            return None
        if self._origins is None:
            return (
                f"{manifest.id} contributes pages and this backend serves no "
                "extension UI, so they could not be shown."
            )
        root = bundle / "ui"
        if not root.is_dir():
            return (
                f"{manifest.id} contributes pages but the bundle has no 'ui' "
                f"directory at {root}, so there is nothing to serve."
            )
        try:
            service = ServiceOrigin.parse(service_url, development=self._context.allow_development)
            await self._origins.serve(
                manifest.id,
                root,
                connect_src=permissions.egress.csp_connect_src(service),
                # The URL as it is, not the derived origin: it is the
                # token's audience, and must be the process's exactly.
                service_url=service_url if service is not None else None,
            )
        except Exception as exc:
            logger.error("serving %s UI failed", manifest.id, exc_info=True)
            return f"its UI could not be served: {exc}"
        return None

    async def drop(self, extension_id: str) -> None:
        """Stop serving this extension's pages."""
        if self._origins is not None:
            await self._origins.drop(extension_id)


class ExtensionPlugin:
    """The plugin an extension carries, and the agents it may serve."""

    def __init__(
        self,
        *,
        plugins_dir: Path | None = None,
        audience: ToolAudience | None = None,
    ) -> None:
        """
        Args:
            plugins_dir: Where a plugin an extension carries is
                installed. ``None`` means this deployment does not take
                them — the extension still installs, and its agents do
                not arrive, which is said at install rather than found
                out.
            audience: Which agents may be offered this extension's
                tools.
        """
        self._plugins_dir = plugins_dir
        self._audience = audience or default_audience

    def install(self, manifest: ExtensionManifest, bundle: Path) -> None:
        """Put the plugin an extension carries where the loader looks.

        Agents, skills and hooks are not contribution types, because
        igni already delivers them through plugins — and a second
        path for the same thing is a second thing to keep in step.
        So an extension *wraps* one, and this copies it into the
        plugins directory under the extension's own id.

        Under the extension's id rather than the plugin's own name:
        uninstalling has to find it again, and a plugin free to call
        itself anything is a plugin that can be left behind, or can
        collide with one somebody installed by hand.

        Failures are logged, not raised. An extension whose service
        works and whose agents did not arrive is degraded; refusing
        the install would be worse, and the line says which it is.
        """
        name = manifest.contributes.plugin
        if not name:
            return
        if self._plugins_dir is None:
            logger.warning(
                "extension %s carries a plugin and this deployment has no "
                "plugins directory configured, so its agents, skills and "
                "hooks will not be loaded.",
                manifest.id,
            )
            return

        source = (bundle / name).resolve()
        # Inside the bundle, checked rather than trusted: the path
        # comes from a manifest, and ``../../..`` is a manifest
        # asking igni to copy somebody else's directory.
        if not source.is_relative_to(bundle.resolve()) or not source.is_dir():
            logger.error(
                "extension %s names a plugin directory that is not in its "
                "bundle: %r. Not installed.",
                manifest.id,
                name,
            )
            return
        if not (source / ".claude-plugin" / "plugin.json").is_file():
            logger.error(
                "extension %s names %r as a plugin and it has no "
                ".claude-plugin/plugin.json, so the loader would skip it "
                "silently. Not installed.",
                manifest.id,
                name,
            )
            return

        target = self._plugins_dir / manifest.id
        try:
            if target.exists():
                shutil.rmtree(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, target)
        except OSError:
            logger.exception("installing %s's plugin failed", manifest.id)
            return
        logger.info("extension %s installed its plugin at %s", manifest.id, target)

    def remove(self, extension_id: str) -> None:
        """Take the carried plugin away with the extension.

        An agent left behind is worse than one that never arrived:
        an agent whose extension is gone is an agent whose every tool
        call fails, offered in the same list as the ones
        that work.
        """
        if self._plugins_dir is None:
            return
        target = self._plugins_dir / extension_id
        if not target.is_dir():
            return
        try:
            shutil.rmtree(target)
        except OSError:  # pragma: no cover — best effort
            logger.exception("removing %s's plugin failed", extension_id)
            return
        logger.info("extension %s's plugin removed from %s", extension_id, target)

    def restrict(self, manifest: ExtensionManifest) -> None:
        """Offer the service only to the agents its plugin ships.

        By directory, not by name: igni does not know which agents
        an extension has or what they are called, only where it put
        them.
        """
        if manifest.permissions.toolAudience != "own-agents":
            return
        if self._plugins_dir is None:
            # Nowhere to have put the agents, so nobody qualifies.
            # Restricting to a directory that does not exist is the
            # honest consequence rather than falling open.
            self._audience.restrict(server_name(manifest.id), Path("/nonexistent"))
            return
        self._audience.restrict(server_name(manifest.id), self._plugins_dir / manifest.id)

    def release(self, extension_id: str) -> None:
        """Stop offering this extension's tools to anybody in particular."""
        self._audience.release(server_name(extension_id))

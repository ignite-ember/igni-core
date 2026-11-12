"""The extension's MCP service: how igni reaches it, and what it may offer.

An extension's service is an MCP server igni connects to. igni builds
its configuration from the extension's manifest when the extension is
installed — it is in nobody's ``.mcp.json`` and in no managed
settings — starts it, and then decides what of what it offers reaches
the model.

The manager igni talks to is named as a protocol
(:class:`~igni_core.extensions.mcp_manager.McpManager`) rather than as
the concrete client manager: an install declares what it needs of it,
so the contract is checked, and so this module stays importable
without the optional ``mcp`` extra.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from igni_core.core.mcp.config import MCPServerConfig, MCPTransport
from igni_core.core.mcp.hints import ToolHints, ToolHintsRegistry
from igni_core.core.mcp.naming import bare_tool_name
from igni_core.extensions.install_environment import ProcessEnvironment
from igni_core.extensions.install_errors import InstallError
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.mcp_manager import McpManager
from igni_core.extensions.permissions import Permissions
from igni_core.extensions.rpc import ExtensionRpc
from igni_core.extensions.service import McpExtensionService

logger = logging.getLogger(__name__)

#: What an extension's MCP server is called inside the MCP manager.
#:
#: Prefixed so an extension cannot shadow a server the user declared
#: in their own ``.mcp.json`` — the id alone would let an install
#: silently take over ``github``.
SERVER_PREFIX = "ext:"

#: How long an extension's service has to start and answer MCP's
#: ``initialize``. Every later call has its own deadline; this is the
#: first one.
START_TIMEOUT_S = 60.0


def server_name(extension_id: str) -> str:
    """The MCP server name for an extension."""
    return f"{SERVER_PREFIX}{extension_id}"


class ExtensionConnection:
    """One extension's service: its configuration, its tools, its lifetime."""

    def __init__(
        self,
        *,
        mcp: McpManager,
        rpc: ExtensionRpc,
        tool_hints: ToolHintsRegistry | None = None,
    ) -> None:
        """
        Args:
            mcp: The MCP client manager this extension's service is
                reached through.
            rpc: Where the extension's own calls to igni are bound.
            tool_hints: Where its tools' hints go for the permission
                modes to read. The process's shared registry by default.
        """
        self._mcp = mcp
        self._rpc = rpc
        self._tool_hints = tool_hints if tool_hints is not None else ToolHintsRegistry.shared()

    def config_for(
        self,
        manifest: ExtensionManifest,
        bundle: Path,
        permissions: Permissions,
        entry_point: Path | None,
        environment: ProcessEnvironment,
    ) -> MCPServerConfig:
        """How to reach this extension's service, within its grants.

        Args:
            entry_point: The installed console script, for ``python``
                packaging.
            environment: The environment the process starts with, as a
                :class:`~igni_core.extensions.install_environment.ProcessEnvironment`.
        """
        name = server_name(manifest.id)

        if manifest.python is not None:
            if entry_point is None:
                raise InstallError(f"{manifest.id}: its package was not installed")
            return MCPServerConfig(
                name=name,
                type=MCPTransport.stdio,
                command=str(entry_point),
                args=[],
                env=environment.merged(),
                cwd=str(bundle),
                source="extension",
            )

        if manifest.dev is not None:
            cwd = bundle if manifest.dev.cwd is None else bundle / manifest.dev.cwd
            return MCPServerConfig(
                name=name,
                type=MCPTransport.stdio,
                command=manifest.dev.command[0],
                args=list(manifest.dev.command[1:]),
                env=environment.with_literals(dict(manifest.dev.env)).merged(),
                # Its command is relative to the bundle, which is not
                # where the backend happens to be running.
                cwd=str(cwd),
                source="extension",
            )

        if manifest.endpoint is not None:
            # igni makes this connection itself, so igni can refuse a
            # destination the manifest never declared.
            permissions.egress.check_endpoint(manifest.endpoint.url)
            return MCPServerConfig(
                name=name,
                type=MCPTransport.sse,
                url=manifest.endpoint.url,
                source="extension",
            )

        raise InstallError(
            f"{manifest.id} is packaged as a sidecar image "
            f"({manifest.sidecar.image if manifest.sidecar else '?'}), and "
            "starting container images is not implemented yet. Use 'python' "
            "to ship a package, 'endpoint' to register a service you run, or "
            "'dev' to run a command locally."
        )

    async def start(self, config: MCPServerConfig) -> str | None:
        """Connect to the service. Returns a reason on failure."""
        try:
            self._mcp.register_server(config)
            result = await asyncio.wait_for(self._mcp.connect(config.name), START_TIMEOUT_S)
        except TimeoutError:
            return f"its service did not start within {START_TIMEOUT_S:g}s"
        except Exception as exc:
            logger.error("extension service %s failed to start", config.name, exc_info=True)
            return f"its service could not be started: {exc}"

        if not result.ok:
            return f"its service did not start: {result.reason or 'no reason given'}"
        return None

    def restrict_tools(
        self, manifest: ExtensionManifest, server: str, permissions: Permissions
    ) -> None:
        """Keep undeclared tools away from the agent.

        Complete enforcement, unlike most of this module: registration
        is igni's, so an extension's service may expose whatever it
        likes and what reaches the model is the manifest's list.

        Done through the same filter a user's own disabling uses,
        because the end state is identical — a tool the agent is not
        offered — and a second mechanism would be a second thing to
        keep in step.

        **The two sides speak different names.** ``get_tools`` returns
        registered names (``mcp_ext:acme.scanner_list``) and the
        manifest declares the server's own (``list``, or
        ``acme.scanner.list``), so every name is read back through
        :func:`~igni_core.core.mcp.naming.bare_tool_name` before it is
        compared with what the manifest declared.
        """
        client = self._mcp.get_client(server)
        if client is None:
            return
        by_bare = {bare_tool_name(server, name): name for name in self._mcp.get_tools(server)}
        refused = permissions.tools.refused(by_bare)
        for tool in refused:
            try:
                self._mcp.set_tool_enabled(server, by_bare[tool], False)
            except OSError as exc:
                # Withheld for this session all the same; only the note
                # that would keep it withheld for the next could not be written.
                logger.warning("could not record %s's withheld tool %s: %s", server, tool, exc)
        if refused:
            logger.info(
                "extension %s offered %d tools its manifest does not declare; not registering %s",
                manifest.id,
                len(refused),
                refused,
            )

    async def apply_hints(
        self, manifest: ExtensionManifest, server: str, permissions: Permissions
    ) -> list[str]:
        """Tell the permission modes what each of its tools is.

        The manifest's hints are the ones that count — an administrator
        approved them — so they go into the registry the permission
        catalog reads, whatever the server says. A tool that is not
        read-only pauses the agent before it runs, so the mode decides:
        plan mode refuses it, Bypass runs it, the default mode asks.

        Then the server is asked what it says, and any tool whose
        reported hints differ is returned as a problem the extensions
        panel shows. Not a failure: the manifest's hints apply, and
        the extension works as it was approved.
        """
        hints: dict[str, ToolHints] = {}
        for registered in self._mcp.get_tools(server):
            declared = permissions.tools.hints(bare_tool_name(server, registered))
            if declared is not None:
                hints[registered] = declared
        self._tool_hints.declare(server, hints)
        self._mcp.require_confirmation(
            server, {name for name, declared in hints.items() if not declared.read_only}
        )

        reported = await self._mcp.tool_annotations(server)
        problems = []
        for registered, declared in sorted(hints.items()):
            bare = bare_tool_name(server, registered)
            said = ToolHints.from_annotations(reported.get(bare))
            if said is not None and said != declared:
                problems.append(
                    f"its tool {bare} reports itself as {said.describe()}, and its "
                    f"manifest says {declared.describe()}; the manifest's hints apply"
                )
        return problems

    def bind(self, extension_id: str, server: str) -> None:
        """Route this extension's calls to its service."""
        self._rpc.bind(extension_id, McpExtensionService(manager=self._mcp, server_name=server))

    def unbind(self, extension_id: str) -> None:
        """Stop routing this extension's calls anywhere."""
        self._rpc.unbind(extension_id)

    async def disconnect(self, extension_id: str) -> None:
        """Close the connection to this extension's service."""
        try:
            await self._mcp.disconnect_one(server_name(extension_id))
        except Exception as exc:  # pragma: no cover — best effort
            logger.warning("stopping %s failed: %s", extension_id, exc)

    def forget_hints(self, extension_id: str) -> None:
        """Forget what this extension's tools were, now it is gone."""
        self._tool_hints.forget(server_name(extension_id))

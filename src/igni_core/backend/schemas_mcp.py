"""Typed schemas for the MCP RPC surface.

Extracted from :mod:`igni_core.backend.server_mcp` so the panel
wire shapes live in one place instead of being hand-rolled as
raw ``list[dict]`` returns inside the controller. Sibling
convention: mirrors :mod:`igni_core.backend.schemas_panels` and
:mod:`igni_core.backend.schemas_hitl` — one file per top-level
concern, ``from_manager`` classmethods that own the domain-
object-to-wire projection.

Consumers:

* :class:`MCPToolToggleResult` — wire return for
  :meth:`McpController.set_tool_enabled`. Moved from the inline
  definition in ``server_mcp.py`` (Rule 1 offender — inline
  Pydantic when a sibling schemas file exists in the package).
* :class:`MCPServerSummary` — the two-field ``(name, connected)``
  row returned by :meth:`McpController.servers`. Replaces the
  raw ``list[dict]`` that the panel had to duck-type. Carries a
  sync :meth:`from_manager` factory.
* :class:`MCPServerSnapshot` — the full nine-field expanded-row
  panel payload returned by :meth:`McpController.server_details`.
  Replaces the raw ``list[dict]`` and encapsulates the projection
  that was inlined in the controller. Because two of the fields
  (``resources`` / ``prompts``) require an ``await`` on the
  manager, its :meth:`from_manager` factory is ``async``.

Field names on :class:`MCPServerSnapshot` are preserved
byte-for-byte from the previous raw-dict shape so
``clients/web/src/protocol/wire-schema.json`` and the
``wire-contract`` test continue to pass without a schema bump.
``resources`` / ``prompts`` were retyped from ``list[dict]`` to
``list[MCPResource]`` / ``list[MCPPrompt]`` — Pydantic serializes
them to the same field-for-field shape so the wire contract is
preserved.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from igni_core.core.mcp.schemas import MCPPrompt, MCPResource

if TYPE_CHECKING:
    from igni_core.core.mcp.client import MCPClientManager
    from igni_core.core.session.mcp_ops import McpFailureCache


class MCPToolToggleResult(BaseModel):
    """Result of enabling or disabling one tool of an MCP server.

    Echoes the tool's new state, so no second call is needed to
    confirm it."""

    server: str = Field(description="Name of the MCP server the tool belongs to.")
    tool: str = Field(description="Name of the tool.")
    enabled: bool = Field(description="The tool's state now: `true` if the agent may use it.")


class MCPServerSummary(BaseModel):
    """An MCP server's name and whether it is connected.

    A cheap listing; use the server details call for tools,
    resources and prompts.
    """

    name: str = Field(description="The server's name, as configured.")
    connected: bool = Field(description="`true` when igni is currently connected to the server.")

    @classmethod
    def from_manager(cls, mgr: MCPClientManager, name: str) -> MCPServerSummary:
        """Project one manager entry into the wire summary."""
        return cls(name=name, connected=name in mgr.list_connected())


class MCPServerSnapshot(BaseModel):
    """Full details of one configured MCP server: connection state,
    tools, resources and prompts."""

    name: str = Field(description="The server's name, as configured.")
    connected: bool = Field(description="`true` when igni is currently connected to the server.")
    transport: str = Field(
        description="How igni talks to the server, e.g. `stdio` or `sse`; `unknown` if it has no config."
    )
    tool_names: list[str] = Field(
        default_factory=list,
        description="Every tool the server offered when it connected, including disabled ones.",
    )
    tool_descriptions: dict[str, str] = Field(
        default_factory=dict, description="Each tool's description, keyed by tool name."
    )
    tools_disabled: list[str] = Field(
        default_factory=list,
        description="Tools the user has turned off; the agent cannot use them.",
    )
    resources: list[MCPResource] = Field(
        default_factory=list,
        description="Resources the server offers; empty when it is not connected.",
    )
    prompts: list[MCPPrompt] = Field(
        default_factory=list,
        description="Prompts the server offers; empty when it is not connected.",
    )
    error: str = Field(default="", description="The last connection error; empty if none.")
    policy_blocked: bool = Field(
        default=False, description="`true` when an administrator policy forbids using this server."
    )

    @classmethod
    async def from_manager(
        cls,
        mgr: MCPClientManager,
        name: str,
        *,
        failures: McpFailureCache | None = None,
    ) -> MCPServerSnapshot:
        """Project one manager entry into the full panel snapshot.

        ``async`` because ``mgr.get_resources`` and
        ``mgr.get_prompts`` round-trip to the MCP session for
        connected servers. Disconnected servers skip both calls
        and get empty lists.

        ``failures`` is the session-scoped :class:`McpFailureCache`
        written by
        :class:`~igni_core.core.session.startup.mcp.McpInitPhase`
        (and the ``/mcp`` command). When omitted the ``error``
        field is empty — matches the pre-refactor "no side
        channel" behaviour for callers that don't track connect
        failures.
        """
        config = mgr.configs.get(name)
        connected = name in mgr.list_connected()
        return cls(
            name=name,
            connected=connected,
            transport=(str(getattr(config.type, "value", config.type)) if config else "unknown"),
            tool_names=mgr.get_tools(name),
            tool_descriptions=mgr.get_tool_descriptions(name),
            tools_disabled=mgr.get_disabled_tools(name),
            resources=await mgr.get_resources(name) if connected else [],
            prompts=await mgr.get_prompts(name) if connected else [],
            error=failures.reason(name) if failures is not None else "",
            policy_blocked=mgr.is_policy_denied(name),
        )

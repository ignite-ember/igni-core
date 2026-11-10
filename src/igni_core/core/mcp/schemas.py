"""Typed wire-shape models for the MCP subsystem.

Matches the sibling ``schemas.py`` convention used by
:mod:`igni_core.core.init`, :mod:`igni_core.core.evals`,
:mod:`igni_core.core.auth`, :mod:`igni_core.core.agents`,
:mod:`igni_core.core.lsp`, :mod:`igni_core.core.hooks`, and
:mod:`igni_core.core.session` — every subsystem's wire shapes
live in one file with ``from_sdk`` / ``from_manager``
classmethods that own the domain-object-to-wire projection.

Contents:

* :class:`MCPResource` / :class:`MCPPrompt` — Pydantic wrappers
  around the MCP SDK's raw resource / prompt objects. Replaces
  the ``list[dict[str, str]]`` return type that
  :meth:`MCPClientManager.get_resources` /
  :meth:`~MCPClientManager.get_prompts` used to hand back
  (Rule 1 offender — raw dicts across the RPC boundary).
* :class:`MCPToolInfo` — packages a tool's name, description
  and enabled state into one wire object so
  :class:`~igni_core.backend.schemas_mcp.MCPServerSnapshot`
  stops stitching three separate manager calls
  (``get_tools`` + ``get_tool_descriptions`` +
  ``get_disabled_tools``) at projection time.
* :class:`MCPConnectResult` — explicit Result type returned by
  :meth:`MCPClientManager.connect`. Replaces the pre-refactor
  ``Any | None`` return + ``self._errors`` side-channel +
  empty-string sentinel (Pattern 3 offender).

The ``from_sdk`` classmethods on :class:`MCPResource` and
:class:`MCPPrompt` accept the MCP Python SDK's raw objects
(``mcp.types.Resource``, ``mcp.types.Prompt``) and coerce
their camelCase fields (``mimeType``) into snake_case
(``mime_type``) so the wire shape stays PEP 8-consistent
with every other :class:`BaseModel` in the codebase.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from agno.tools.mcp import MCPTools


class MCPResource(BaseModel):
    """One resource an MCP server offers (a file, document or other data it can return).

    Only ``uri`` is always set; the other fields are empty when the
    server does not provide them.
    """

    uri: str = Field(description="URI that identifies the resource on its server.")
    name: str = Field(
        default="", description="Human-readable name of the resource. Empty when not given."
    )
    description: str = Field(
        default="", description="What the resource contains. Empty when not given."
    )
    mime_type: str = Field(
        default="",
        description="MIME type of the resource content, e.g. `text/plain`. Empty when unknown.",
    )

    @classmethod
    def from_sdk(cls, resource: Any) -> MCPResource:
        """Project an ``mcp.types.Resource`` SDK object into the
        wire model, tolerating ``None`` on any optional attribute."""
        return cls(
            uri=str(getattr(resource, "uri", "") or ""),
            name=getattr(resource, "name", "") or "",
            description=getattr(resource, "description", "") or "",
            mime_type=getattr(resource, "mime_type", "") or "",
        )


# ``arguments`` carries only names; if a consumer needs the type /
# required-flag columns, add fields here and update from_sdk.
class MCPPrompt(BaseModel):
    """One prompt template an MCP server offers."""

    name: str = Field(description="Name of the prompt on its server.")
    description: str = Field(
        default="", description="What the prompt is for. Empty when not given."
    )
    arguments: list[str] = Field(
        default_factory=list,
        description="Names of the arguments the prompt takes. Empty when it takes none.",
    )

    @classmethod
    def from_sdk(cls, prompt: Any) -> MCPPrompt:
        """Project an ``mcp.types.Prompt`` SDK object into the
        wire model. Flattens the ``prompt.arguments`` list of
        argument objects to their ``.name`` strings."""
        raw_args = getattr(prompt, "arguments", None) or []
        return cls(
            name=getattr(prompt, "name", "") or "",
            description=getattr(prompt, "description", "") or "",
            arguments=[getattr(a, "name", "") or "" for a in raw_args],
        )


class MCPToolInfo(BaseModel):
    """Packaged wire shape for one MCP tool row.

    Replaces the ``get_tools`` + ``get_tool_descriptions`` +
    ``get_disabled_tools`` triple-call composition that
    :class:`~igni_core.backend.schemas_mcp.MCPServerSnapshot`
    used to perform at projection time. A single manager call
    (:meth:`MCPClientManager.list_tool_info`) now hands back
    a list of these — one row per tool — with the three
    columns pre-joined.
    """

    name: str
    description: str = ""
    enabled: bool = True


class MCPConnectResult(BaseModel):
    """Explicit Result type returned by
    :meth:`MCPClientManager.connect`.

    Replaces the pre-refactor ``Any | None`` + ``self._errors``
    side-channel with a single value that carries success /
    failure and the reason together (Pattern 3 offender fix).

    * ``ok`` — ``True`` on successful connect.
    * ``client`` — the live ``agno.tools.mcp.MCPTools`` handle
      on success, or ``None`` on failure. Excluded from
      :meth:`model_dump` so a serialization pass never walks
      the live subprocess-owning handle.
    * ``reason`` — human-readable failure explanation on
      ``ok=False``; empty string on success.

    Use :meth:`success` / :meth:`failure` classmethod
    constructors so every failure branch in
    :meth:`~MCPClientManager.connect` reads as
    ``return MCPConnectResult.failure("...")`` and callers
    can't accidentally construct an ``ok=True`` Result with a
    non-empty ``reason``.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    ok: bool
    client: Any = Field(default=None, exclude=True)
    reason: str = ""

    @classmethod
    def success(cls, client: MCPTools) -> MCPConnectResult:
        """Build a ``ok=True`` Result carrying the live MCPTools
        handle. Callers pull the client via ``result.client``."""
        return cls(ok=True, client=client, reason="")

    @classmethod
    def failure(cls, reason: str) -> MCPConnectResult:
        """Build a ``ok=False`` Result carrying only the reason
        string. The client is left as ``None`` so callers can
        safely branch on ``result.ok`` before touching
        ``result.client``."""
        return cls(ok=False, client=None, reason=reason)

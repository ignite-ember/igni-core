"""What igni asks of the MCP client manager, and what it asks of a service.

The extension subsystem talks to the MCP client manager in two ways:
an install configures an extension's service and starts it, and an
extension's own service is later reached through the manager's live
client handle. Both are contracts, and both are declared here as
protocols rather than as the concrete
:class:`~igni_core.core.mcp.client.MCPClientManager` — so the code
that depends on them is checked, and so the optional ``mcp`` extra
stays optional for the modules that import these.

The module lives apart from
:mod:`igni_core.extensions.install_service` and
:mod:`igni_core.extensions.service` because both of those need it, and
neither may import the other.

The client handle is typed as :data:`typing.Any`: it is an opaque
live handle owned by the MCP SDK, and the only thing igni does with it
is call it.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from igni_core.core.mcp.config import MCPServerConfig
from igni_core.core.mcp.schemas import MCPConnectResult


@runtime_checkable
class McpManager(Protocol):
    """The slice of the MCP client manager an extension install uses.

    The concrete manager is
    :class:`~igni_core.core.mcp.client.MCPClientManager`; this is what
    an install depends on, declared so the contract is checked and so
    the optional ``mcp`` extra stays optional for the modules that
    import it.
    """

    def register_server(self, config: MCPServerConfig) -> None:
        """Add a server igni configures itself, to connect it by name."""
        ...

    def get_client(self, name: str) -> Any:
        """The connected client for ``name``, or ``None``."""
        ...

    def get_tools(self, name: str) -> list[str]:
        """The registered tool names of ``name``."""
        ...

    def set_tool_enabled(self, server: str, tool: str, enabled: bool) -> None:
        """Turn one tool on or off."""
        ...

    def require_confirmation(self, server: str, tools: set[str]) -> None:
        """Pause the agent before each of ``tools`` runs."""
        ...

    async def tool_annotations(self, name: str) -> dict[str, Any]:
        """What the server says about its own tools."""
        ...

    async def connect(self, name: str) -> MCPConnectResult:
        """Connect to the server called ``name``."""
        ...

    async def disconnect_one(self, name: str) -> bool:
        """Disconnect the server called ``name``."""
        ...


@runtime_checkable
class McpServiceManager(Protocol):
    """The slice of the manager an extension's own service is reached through.

    Narrower than :class:`McpManager` on purpose: reaching a service
    that is already connected needs one method, and a dependency that
    asks for one is a dependency that is easy to satisfy.
    """

    def get_client(self, name: str) -> Any:
        """The connected client for ``name``, or ``None``."""
        ...
